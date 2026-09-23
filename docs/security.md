# Политика безопасности и приватности

Этот документ детализирует обязательный корневой `SECURITY.md`. Корневой файл —
короткий merge-blocking checklist; здесь описаны rationale, threat model и runtime
ограничения. При расхождении сначала применяется внешний API-контракт, затем
`SECURITY.md`; расхождение должно быть устранено отдельным изменением документации.

## Защищаемые данные

К защищаемым относятся исходные payload/prompt, найденные значения PII, mapping,
demasked output и session `/process`. Placeholder без значения может логироваться
только если не позволяет восстановить PII.

## Запрещено в production logs и metrics

- полный request/response body;
- raw prompt или payload;
- `PIIMatch.value`;
- mapping placeholder -> original;
- original/masked session pair;
- fragments текста вокруг найденного span;
- exception repr, если библиотека может включить входной текст.

## Разрешённая диагностика

- внутренний request/correlation ID;
- backend ID/URL без credentials;
- status и error type;
- latency;
- количество кандидатов и масок;
- типы PII;
- confidence и action без value/offset-context;
- queue depth, RPS, TPS, cache hit/miss и store size.

`payload_id` не следует считать секретом, но предпочтительно логировать hash/truncated
representation, чтобы не переносить внешние идентификаторы между системами.

## Dev-only full body logging

`MOCK_LOG_REQUEST_BODY=true` допускается только локально, только для синтетических
fixtures и только в mock backend. Значение по умолчанию — `false`. На evaluation,
demo с пользовательскими данными и production-like стендах флаг запрещён.

## Локальный NER

- Inference выполняется внутри контролируемого контура.
- Checkpoint и revision должны быть закреплены. Default revision задаётся константой,
  передаётся в оба вызова `from_pretrained` и может быть осознанно заменена через
  `NER_MODEL_REVISION`.
- Модель загружается один раз на startup.
- После подготовки model cache используются `HF_HUB_OFFLINE=1` и
  `TRANSFORMERS_OFFLINE=1`.
- Пользовательский текст не отправляется во внешние LLM/API.
- Unit-тесты используют fake backend и не скачивают модель.

## State `/process`

- Session хранится минимально необходимое время.
- Store имеет отдельные TTL для ACTIVE и COMPLETED, entry bound, approximate byte
  bound и управляемую очистку.
- Лимит 100 000 токенов нельзя подменять приблизительным лимитом 400 КБ: русский
  UTF-8 текст и разные tokenizer дают другое соотношение. Byte и token limits
  конфигурируются и проверяются отдельно.
- При превышении лимита `/process` возвращает `413` с пояснением в стиле DeepSeek
  (максимальный лимит vs фактическое значение), без раскрытия самого payload.
- После demasking session не удаляется мгновенно: короткий COMPLETED TTL нужен для
  безопасного retry, если ответ потерян после обработки.
- Очистка не пишет содержимое session в лог.
- Несовпадающий payload не перезаписывает существующую session.
- При использовании внешнего store транспорт и данные должны быть защищены; raw PII
  не хранится бессрочно.
- Несколько workers не запускаются с независимыми in-memory stores.

## Fail-closed

Если обязательный detector/model недоступен или pipeline завершился частично,
потенциально необработанный текст нельзя отправлять во внешнюю LLM. API возвращает
контролируемую ошибку без пользовательского текста. Rule-only degradation допустима
только для consumer profile, явно разрешающего такой режим.

Для `/process` fail-closed менее критичен, поскольку endpoint не отправляет данные
во внешнюю LLM: он возвращает маску/оригинал напрямую. При недоступности детектора
`/process` возвращает контролируемую ошибку (5xx), а не необработанный текст как
«маску»: `ProcessService.process` перехватывает `BaseException` и возвращает
`ProcessError("processing failed")`. Это покрыто тестом
`test_process_fail_closed_on_detector_error`. Rule-only degradation (`rule_only`)
доступна для consumer profile, явно разрешающего такой режим (ADR-0004).

## Тестовые данные

- Использовать `example.com`/`example.org` и заведомо синтетические данные.
- Для карт использовать публичные test numbers, а не реальные реквизиты.
- Не копировать персональные документы в fixtures, issues, PR или README.
- Quality dataset хранить без реальных персональных данных.

## Review checklist

- Может ли новый лог содержать входной текст через `%s`, repr или exception?
- Может ли ошибка вернуть payload клиенту?
- Есть ли новый внешний сетевой вызов?
- Не увеличился ли срок жизни raw PII?
- Сохраняется ли fail-closed поведение?
- Выключен ли dev logging в deployment configuration?
