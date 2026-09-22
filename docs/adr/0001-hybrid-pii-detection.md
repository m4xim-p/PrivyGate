# ADR-0001: Гибридная PII-детекция

- Статус: Accepted
- Дата: 2026-09-22

## Контекст

Структурированные идентификаторы хорошо определяются правилами и checksum, а ФИО,
адреса и контекстные поля требуют языковой модели или контекстных эвристик. Полностью
regex-подход имеет низкий recall на свободном тексте; полностью ML-подход сложнее
объяснить, медленнее и создаёт false positives на документах и числовых кодах.

## Решение

Использовать единый `PIIDetector` protocol и гибридный registry:

- regex/rules/checksum для структурированных типов;
- context scoring для неоднозначных кандидатов;
- optional локальный NER для PERSON и будущих неструктурированных типов;
- общий confidence threshold и overlap resolver после всех detectors;
- внешняя генеративная LLM не используется для PII detection.

## Последствия

Плюсы:

- detector можно добавлять без изменения endpoints;
- rule decisions объяснимы и быстры;
- ML можно включать только в необходимых profiles;
- общий overlap resolver предотвращает двойное masking одного span.

Минусы:

- thresholds и context weights требуют quality dataset;
- разные detectors могут конфликтовать;
- локальный NER является основным performance bottleneck;
- модель и rule configuration должны версионироваться вместе с отчётом качества.

## Ограничения реализации

- Detector возвращает исходные offsets и не изменяет текст.
- Detector не логирует `value`.
- Masking format выбирается после detection отдельной policy.
- Unit-тесты NER используют fake backend.
