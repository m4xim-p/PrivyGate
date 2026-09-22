# ADR-0003: Typed placeholders для профиля AlfaSonar

- Статус: Accepted
- Дата: 2026-09-22

## Контекст

Организаторы уточнили, что эталонной маски нет. Span-based scorer оценивает, скрыты
ли PII spans относительно original text; replacement может иметь любую длину и состоять
из любых символов. Частично открытые PII получают немного меньшую оценку, false negative
штрафуется сильнее false positive, но избыточное masking неперсонального текста также
штрафуется.

## Решение

Для default evaluation profile `alfasonar` оставить typed placeholders:

```text
__PII_PERSON_1__
__PII_PASSPORT_1__
__PII_CARD_1__
```

Detector должен возвращать точные границы PII. Служебные слова (`серия`, `ул.`,
`года`) не входят в masked span, если сами не являются частью PII value. Если весь
payload является одним PII value, маскирование всей строки допустимо.

## Последствия

Плюсы:

- текущий masker можно переиспользовать в `/process`;
- mapping и точное demasking уже реализованы;
- длина placeholder не ухудшает scorer;
- тип placeholder помогает безопасной диагностике и overlap analysis.

Минусы:

- качество зависит от точности span boundaries;
- повреждённый LLM placeholder может не восстановиться в proxy;
- format-preserving/synthetic masking всё ещё требуется для отдельных consumer
  profiles, но не является приоритетом автоматической проверки.

## Не делать

- Не добавлять partial-preserving mask только ради AlfaSonar.
- Не сохранять открытые символы PII ради визуального сходства с примером OpenAPI.
- Не маскировать весь payload как способ искусственно поднять recall.
