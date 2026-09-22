# Контракт автоматической проверки AlfaSonar

Этот документ фиксирует внешний контракт. Внутренняя реализация может меняться,
но формат запроса, ответа и семантика корреляции сохраняются. Машинно-читаемая
OpenAPI-спецификация — [`process_api.yaml`](../process_api.yaml).

Нормативная база: Приложения A/B в
[`source/track-specification.txt`](source/track-specification.txt). Неоднозначности
mask scoring, нагрузки, retries и доступа разрешены сохранёнными
[`ответами организаторов`](source/organizer-clarifications.txt). Этот документ является
производным и не может переопределять эти источники.

## HTTP API

```http
POST /process
Content-Type: application/json
```

Request:

```json
{
  "payload": "Клиент Иванов Иван Иванович, паспорт 4509 123456",
  "payload_id": "8a77d363c7c044b49b41d7b8a448243a"
}
```

Successful response:

```json
{
  "result": "Клиент __PII_PERSON_1__, паспорт __PII_PASSPORT_1__"
}
```

Оба поля request обязательны и имеют тип string. Единственное обязательное поле
response — string `result`.

## Внешняя state machine и idempotency

| Состояние | Вход | Действие | Ответ |
|---|---|---|---|
| ID отсутствует | любой payload | Mask, атомарно сохранить session | `masked` |
| ID существует | `payload == original` | Retry masking | тот же `masked` |
| ID существует | `payload == masked` | Demasking или его retry | `original` |
| ID существует | иной payload | Не менять session | `409 Conflict` |

Если `original == masked`, возврат исходной строки корректен на обоих шагах.
Генерация маски должна быть детерминированной внутри session.

Два конкурентных первых запроса с одинаковым ID и одинаковым payload должны получить
одинаковый результат. Два конкурентных запроса с одинаковым ID и разными payload не
должны перезаписывать session друг друга.

## Внутренний lifecycle session

```text
ABSENT
  -> masking
  -> ACTIVE(original, masked, diagnostics)
  -> demasking
  -> COMPLETED(original, masked)
  -> expiry
```

`ACTIVE` сохраняет original и masked до первого успешного demasking и допускает retry
исходного payload. `COMPLETED` нельзя удалять немедленно: если demasking response
потерян в сети, повтор masked payload должен снова вернуть original. Для состояний
используются разные TTL и единый atomic store.

Mapping в session не хранится: он существует только временно внутри `PIIMasker.mask()`
и удаляется после формирования masked. Demasking выполняется возвратом сохранённого
original, поскольку checker присылает точную строку masked.

`COMPLETED` отвечает на оба входа: повтор исходного payload возвращает ту же маску,
повтор masked payload возвращает original.

## Ошибки

- `422` — malformed JSON, отсутствует поле или нарушен тип schema;
- `409` — существующий `payload_id` использован с неизвестным payload;
- `410` — известный `payload_id` истёк и защищён tombstone;
- `413` — payload превышает поддерживаемый лимит размера;
- `429` — admission limit исчерпан; обязательно добавить `Retry-After`;
- `5xx` — внутренняя ошибка, без raw payload в response или logs.

При истёкшем TTL нельзя принимать известный ранее masked payload как новый original.
Предлагается хранить короткий tombstone/hash `payload_id` и возвращать `410 Gone`; точное
решение и сроки фиксируются в ADR-0002 перед переводом его в `Accepted`.

## Параметры проверки

- нагрузочный прогон длится примерно 5 минут и использует разгон до 1000 RPS с
  удержанием достигнутого уровня; точные длительности ступеней не раскрыты;
- организаторы также указали средний профиль около 330 RPS с пиками до 1000 RPS;
- одновременно используется до 200 соединений; каждое ждёт ответ перед следующим
  запросом;
- 1000 RPS означает отдельные HTTP-запросы, а не пары;
- masking и demasking запросов примерно поровну;
- целевая latency — не более 1 секунды; фиксируются mean, p50, p95 и p99;
- hard timeout запроса — 10 секунд;
- до двух retries после первой попытки;
- `429` не считается ошибкой и обрабатывается checker с ожиданием, но сохраняется в
  статистике;
- после пяти невалидных ответов подряд прогон останавливается;
- элементы dataset могут переиспользоваться;
- masking оценивается кастомной span-based метрикой относительно original text;
- demasking сравнивается с исходной строкой.

## Mask compatibility

Эталонной маски и обязательного mask format нет. Допустимы любые символы или группы
символов, включая текущие typed placeholders; длина replacement не влияет на score.
Разделители внутри PII можно оставлять или скрывать.

Правила качества:

- полное скрытие PII немного лучше частичного раскрытия символов;
- false negative штрафуется сильнее false positive;
- blanket masking неперсонального текста штрафуется;
- если весь payload является одним PII value, полная маска всей строки корректна;
- захват служебных слов (`серия`, `ул.`, `года`) является избыточной маской.

Default профиль `alfasonar` сохраняет typed placeholders и фокусируется на точных
границах PII spans. Format-preserving и synthetic masking остаются дополнительными
per-consumer возможностями, но не являются P0/P1 для автоматической проверки.

## Demasking behavior

Checker всегда отправляет на demasking маску без изменений — именно строку из ответа
masking с тем же `payload_id`. Проверка `/process` не имитирует редактирование текста
внешней LLM. Количество demasking запросов совпадает с количеством masking запросов.

`payload_id` уникален для пары и повторяется только для demasking или retry после
ошибки/`429`. Срок хранения выбирает команда; он должен покрывать парный вызов и retries.

## Evaluation access и code scan

Checker отправляет `/process` без согласованных auth headers. Endpoint использует
default evaluation profile; отсутствие защиты именно этого endpoint не штрафуется.

Автоматический code scan содержит множество правил по complexity, DRY/KISS,
conventions, deprecated APIs/dependencies и общему качеству. Детальный отчёт и точные
пороги не предоставляются; по последнему уточнению проверка формально считается
успешной, но качество кода остаётся частью экспертной оценки.

## Deployment checklist

- `/process` доступен по предоставленному HTTP/HTTPS URL;
- используется один state owner либо shared store;
- unit/integration tests зелёные;
- dev full-body logging выключен;
- модель доступна локально, runtime не зависит от Hugging Face Hub;
- проверены timeout, retry, `429` и `Retry-After`;
- выполнен rate-controlled benchmark;
- процесс остаётся доступен на время всего прогона.
