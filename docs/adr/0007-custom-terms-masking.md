# ADR-0007: Custom terms masking per consumer

- Статус: Accepted
- Дата: 2026-09-23

## Контекст

Система-потребитель может потребовать маскировать определённые термины/слова,
которые не являются стандартными категориями ПДН (например, внутренние кодовые
слова, названия продуктов, специфичные для системы данные). Это должно работать
**только для этой системы**, а не глобально.

Текущая архитектура (ADR-0004) поддерживает per-consumer настройку через
`ConsumerPolicy` (enabled/excluded_pii_types, masking_mode, degradation,
require_card_for_pin), но не поддерживает произвольные пользовательские термины.

## Источники решения

- `docs/architecture.md` (единое PII-ядро, разрешённые зависимости).
- ADR-0001 (гибридная детекция, `PIIDetector`), ADR-0003 (typed placeholders),
  ADR-0004 (consumer policy registry), ADR-0005/0006 (hot path).
- `SECURITY.md` (не логировать PII, единое ядро, fail-closed).

## Решение

### 1. Новая категория `CUSTOM_TERM`

Вводится новая категория маскирования `CUSTOM_TERM`. Она **не входит** в
`_ALL_PII_TYPES` (17 обязательных категорий) — иначе default профиль `alfasonar`
получит пустой детектор и лишнюю категорию в диагностике. `CUSTOM_TERM`
активна только когда у consumer заданы `custom_terms`.

### 2. `CustomTermDetector` (app/pii.py)

Новый `PIIDetector`:

```python
class CustomTermDetector:
    pii_type = "CUSTOM_TERM"
    def __init__(self, terms: Sequence[str], confidence: float = 1.0) -> None: ...
    def detect(self, text: str) -> list[PIIMatch]: ...
```

- Матчит термины как **точные слова** (word-boundary, case-insensitive).
- Возвращает `PIIMatch(pii_type="CUSTOM_TERM", confidence=1.0, start, end, value)`.
- **Не логирует** value и сами термины (конфиденциальные данные системы).
- Stateless в `detect()` — безопасно кэшировать (ADR-0005/0006).

### 3. `ConsumerPolicy.custom_terms` (app/policy.py)

Новое per-consumer поле:

```python
@dataclass(frozen=True)
class ConsumerPolicy:
    ...
    custom_terms: tuple[str, ...] = ()
```

Парсится из JSON-конфига (`custom_terms: string[]`). Загружается через
`PolicyRegistry` без редеплоя (как остальные поля).

### 4. `PIIMaskingEngine.mask` (app/pii_engine.py)

При создании `PIIMasker` добавляется `CustomTermDetector(policy.custom_terms)`,
если термины непустые. Встраивается в единый pipeline — кандидаты проходят тот
же путь: фильтрация по confidence → `resolve_overlapping_matches` → маскирование
→ mapping → demasking. **Не создаётся второй pipeline.**

### 5. `PIIMasker._is_enabled` (app/pii.py)

`CUSTOM_TERM` учитывается отдельно от `enabled_pii_types`: если `custom_terms`
непустые, `CUSTOM_TERM` всегда маскируется, даже если не входит в
`enabled_pii_types`. Иначе термины отфильтруются.

### 6. Overlap-приоритет

`confidence = 1.0` для custom terms (пользователь явно просил маскировать).
При overlap с реальным PII custom term выигрывает (выше confidence). Это
документируется как ожидаемое поведение.

### 7. Поведение `/process`

`/process` не имеет consumer identity и использует default профиль `alfasonar`
без `custom_terms`. Фича активна только для product API
(`/v1/chat/completions`), где policy резолвится по `X-Consumer-ID`.

### 8. Затрагиваемые компоненты

- `app/pii.py` — `CustomTermDetector`, правка `PIIMasker._is_enabled`.
- `app/policy.py` — поле `custom_terms` + парсинг.
- `app/pii_engine.py` — добавление детектора из policy.
- `config/policy.example.json` — пример поля `custom_terms`.
- `docs/` — requirements, architecture, README.

## Последствия

Плюсы:

- per-consumer маскирование произвольных терминов без правки ядра;
- встраивается в единое PII-ядро — нет второго pipeline;
- demasking и masking mode работают автоматически;
- `/process` (default alfasonar) не затрагивается.

Минусы:

- `CUSTOM_TERM` не в `_ALL_PII_TYPES` — нужна отдельная ветка в `_is_enabled`;
- overlap-приоритет custom terms над реальным PII может перебить детектор;
- термины — конфиденциальные данные, нельзя логировать.

## Условия принятия

1. **Тесты**: positive, negative, case/format, overlap, round-trip; per-consumer
   изоляция (термины маскируются только для заданного consumer); `/process`
   не маскирует custom terms; отсутствие терминов в логах.
2. **check.sh** зелёный (Ruff, mypy, Bandit, pip-audit, pytest).

## Не делать

- Не добавлять `CUSTOM_TERM` в `_ALL_PII_TYPES`.
- Не создавать второй PII pipeline.
- Не логировать термины и их значения.
- Не менять контракт `/process`.
- Не добавлять regex-термины (только точные слова) без отдельного ADR.