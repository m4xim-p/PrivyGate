# Требования и матрица покрытия

## Цель продукта

PrivyGate защищает персональные данные в цепочке «система-потребитель — LLM»:
идентифицирует PII, маскирует его до передачи модели и при разрешённой политике
восстанавливает данные в ответе. Решение должно использоваться и как прозрачный
LLM proxy, и как отдельный сервис обработки строк по контракту AlfaSonar.

## Целевые показатели

| Показатель | Цель | Текущий статус |
|---|---:|---|
| Качество identification/masking/demasking | не менее 95% | Строгий entity/span harness; актуальные F1 по датасетам — в [docs/benchmarks/quality-baseline.md](benchmarks/quality-baseline.md). Целевые 95% не достигнуты |
| Нагрузка | разгон до 1000 RPS с удержанием; в уточнении также указана средняя ~330 RPS | После ADR-0006: полный 5-мин прогон, hold-фаза ~999.5 HTTP RPS, mask p95 9ms, dropped_iterations ~0 на hold. Tombstone лимит 100k, при переполнении новые ID → 429. Цель 1000 RPS достигнута |
| Соединения | до 200 параллельных, последовательные запросы внутри соединения | k6 benchmark: 200 VU, все запросы успешны (0 ошибок, 0 retries, 0 429) |
| Latency | не более 1 секунды; фиксируются mean/p50/p95/p99 | После ADR-0006: mask p50 1.57ms / p95 7.68ms / p99 101ms, demask p50 0.775ms / p95 3.50ms / p99 24.47ms. Latency ≤1s достигнута |
| Размер текста | до 100 000 токенов | Реализована раздельная валидация bytes и estimated tokens (не «100k = 400 КБ»); обработка 100k токенов не подтверждена нагрузкой |
| Расширенный уровень | 2000 RPS | Не реализовано |
| Автоматический контракт | `POST /process` | present | Реализован: ProcessService + ProcessStore, контрактные тесты зелёные |

## Обязательные категории PII

Статусы:

- `present` — detector включён в default registry и покрыт unit-тестами;
- `partial` — реализация есть, но форматы/контекст или quality baseline неполны;
- `missing` — реализации нет;
- `validated` — подтверждено на согласованном golden dataset (пока нет таких строк).

| Категория | Кодовый тип | Статус | Основной пробел |
|---|---|---|---|
| ФИО | `PERSON` | present | Rule-based по датасету + контекстные маркеры (ФИО:, зовут, клиент) для незнакомых имён; recall 1.000 на api_dataset |
| Дата рождения | `DATE_OF_BIRTH` | partial | Нужен полный набор форматов и отделение от прочих дат |
| Место рождения | `BIRTH_PLACE` | partial | Сейчас context/rule extraction; нужен quality baseline |
| Паспорт РФ | `PASSPORT` | present | РФ (серия+номер), загранпаспорт (2+7 цифр), паспорт иностранного гражданина (2 буквы+7 цифр) |
| Гражданство | `CITIZENSHIP` | partial | Context extraction может захватывать лишний текст |
| Орган выдачи паспорта | `PASSPORT_AUTHORITY` | partial | Нужны вариации формулировок и точные границы span |
| Код подразделения | `PASSPORT_UNIT_CODE` | partial | Нужны negative-context и форматные тесты |
| Дата выдачи паспорта | `PASSPORT_ISSUE_DATE` | partial | Конфликтует с общим detector дат рождения |
| Водительское удостоверение | `DRIVING_LICENSE` | partial | Нужны дополнительные допустимые форматы и negatives |
| Адрес и компоненты | `ADDRESS` | present | Двухстадийный детектор: якоря + расширение границ; точные spans (38/38 на api_dataset), гранулы (zip/region/city/street/house/flat) |
| Email | `EMAIL` | present | Требуется corpus-level validation |
| Телефон | `PHONE` | present | Корпоративные номера (8-800, служба поддержки) исключаются через negative-контекст |
| ИНН | `INN` | present | Требуется corpus-level validation; checksum реализован |
| Номер карты | `CARD` | present | Требуется corpus-level validation; Luhn реализован |
| CVV/CVC | `CVV` | partial | Нужны комбинационные правила с картой |
| PIN-код | `PIN` | present | Комбинационное правило с картой реализовано и настраивается через `require_card_for_pin` |
| Имя держателя карты | `CARD_HOLDER` | partial | Сейчас ориентировано на uppercase Latin и явный контекст |

Дополнительно реализованы детекторы `SNILS` (с checksum), `KPP` и `OGRN`, хотя
они не входят в обязательные 17 категорий текущего задания. Их типы **не входят**
в `_ALL_PII_TYPES`, поэтому по умолчанию (default profile `alfasonar`) они **не
маскируются** в `/process`; включаются через `enabled_pii_types` профиля
потребителя.

## Функциональные требования

| Требование | Статус | Комментарий |
|---|---|---|
| Request-scoped masking/demasking | present | Используется LLM proxy |
| Boundary-safe streaming demasking | present | Placeholder может пересекать chunks |
| `POST /process` | present | Реализован: ProcessService + ProcessStore |
| Idempotency по `payload_id` | present | Retry masking/demasking детерминированы; конфликт -> 409 |
| Конфигурация типов PII по потребителю | present | PolicyRegistry (ADR-0004): per-consumer enabled_pii_types |
| Включение/отключение потребителей | present | PolicyRegistry: allowlist + enabled флаг (ADR-0004) |
| Демаскирование по политике потребителя | present | ConsumerPolicy.allow_demasking (ADR-0004) |
| Маска AlfaSonar | present | Typed placeholders допустимы; нужно измерить точность PII spans |
| Выбор masking strategy по потребителю | present | masking_mode: typed_placeholder, synthetic, format_preserving |
| Custom terms маскирование по потребителю | present | ConsumerPolicy.custom_terms (ADR-0007): per-consumer список терминов, маскируются только для заданной системы |
| Независимость от регистра | partial | Реализовано не во всех detector одинаково |
| Контекстные комбинационные правила | partial | Есть context scoring, нет общего policy engine |
| Ловушки «Пушкин» и адрес банка | present | Known-person suppression (включая ФИО с отчеством); адрес организации исключается через negative-контекст |
| Безопасные логи типов PII | present | Нельзя считать заменой metrics |
| Latency/RPS/TPS metrics | present | `/metrics` endpoint (Prometheus text): счётчики mask/demask/retry/429, store size, event loop delay, uptime; RPS/latency собираются k6 |
| Ошибки и деградация | present | Fail-closed и rule_only degradation (ADR-0004) |
| Реальная LLM upstream | present | `UpstreamClient` + `ModelRegistry` (ADR-0008): маршрутизация по `model`, `X-Model-API-Key`, mock fallback, fail-closed 502 |
| Квоты токенов per-client | present | `ConsumerPolicy.max_tokens_per_request` → 429 при превышении |
| Ограниченный список систем | present | Allowlist для product API (ADR-0004); evaluation /process без auth |

## Нефункциональные ограничения

Детальные правила и rationale — в [SECURITY.md](../SECURITY.md),
[docs/security.md](security.md), [docs/evaluation-contract.md](evaluation-contract.md)
и [docs/architecture.md](architecture.md). Здесь — сводка требований.

- Runtime inference должен выполняться локально в защищённом контуре.
- Raw PII, prompt и mapping не попадают в production logs или metrics.
  (подробно — [SECURITY.md](../SECURITY.md), [docs/security.md](security.md)).
- Архитектура должна добавлять detector через registry, не через изменения endpoint.
  (подробно — [docs/architecture.md](architecture.md)).
- `/process` должен выдерживать retries, возвращать `429` с `Retry-After` при
  контролируемой перегрузке и не зависать до hard timeout.
- `429` не считается ошибкой checker, но учитывается в статистике и не должен быть
  постоянным состоянием.
- RPS считается по отдельным HTTP-запросам; masking и demasking представлены примерно
  поровну.
- На demasking checker передаёт без изменений маску, полученную от сервиса.
- Любая replacement-группа допустима, длина маски не влияет на score. Полное скрытие
  PII предпочтительнее частичного; захват служебных слов и blanket masking штрафуются.
  (подробно — [docs/evaluation-contract.md](evaluation-contract.md)).
- Для масштабирования stateful `/process` требуется согласованная стратегия state store.
- Store ограничивается по числу entries и суммарному приблизительному размеру в байтах;
  после demasking session кратко сохраняется для безопасного retry.
  (подробно — [docs/security.md](security.md)).
- Все примеры и fixtures содержат только синтетические данные.
- Каждый push и PR проходит merge-blocking gate: Ruff (lint/complexity/deprecated
  syntax), mypy (typing), Bandit (Python SAST), pip-audit (dependency CVE) и pytest.
  (подробно — [AGENTS.md](../AGENTS.md), [SECURITY.md](../SECURITY.md)).

## Не является текущей целью

- Kubernetes, Kafka и сложная распределённая инфраструктура без измеренной необходимости.
- Внешняя генеративная LLM для обнаружения PII.
- Production-ready банковская сертификация в рамках MVP.
- Дублирование PII pipeline ради отдельного endpoint.
  (подробно — [docs/architecture.md](architecture.md)).

## Правило обновления

Изменение статуса на `present` требует unit/integration tests. Статус `validated`
разрешён только после сохранения воспроизводимого отчёта quality evaluation с
описанием dataset, метрик и версии конфигурации.
