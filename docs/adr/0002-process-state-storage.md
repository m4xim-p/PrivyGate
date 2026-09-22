# ADR-0002: Хранилище состояния `/process`

- Статус: Proposed
- Дата: 2026-09-22

## Контекст

AlfaSonar вызывает `/process` дважды с одинаковым `payload_id`: сначала для masking,
затем для demasking. Между запросами необходимо сохранить original, masked и mapping.
Checker повторяет запросы, поэтому state transition должна быть идемпотентной.
Уточнённый профиль — до 200 параллельных соединений, последовательная пара
masking/demasking внутри соединения, средняя нагрузка около 330 RPS и пики до 1000.

## Предлагаемое решение для первого этапа

Использовать bounded in-memory TTL store в одном Uvicorn process:

- atomic get-or-create по `payload_id`;
- lifecycle `ACTIVE -> COMPLETED -> expired`;
- `ACTIVE`: original, masked, mapping, created/last-access timestamps;
- `COMPLETED`: original и masked сохраняются на короткий retry TTL;
- детерминированный ответ на retry;
- отдельные ACTIVE/COMPLETED TTL;
- max entries и approximate max bytes;
- controlled overload вместо неограниченного роста;
- raw session data не логируется.

Предварительные значения для первой реализации, которые должны быть подтверждены
benchmark:

- `PROCESS_ACTIVE_TTL_SECONDS=900` — покрывает пятиминутный прогон и retries;
- `PROCESS_COMPLETED_TTL_SECONDS=120` — позволяет повторить demasking после потери ответа;
- `PROCESS_STORE_MAX_ENTRIES=25000` — соответствует средней нагрузке с запасом;
- `PROCESS_STORE_MAX_BYTES=536870912` (512 MiB) — защищает процесс от OOM;
- при исчерпании entry/byte limit возвращается `429` с `Retry-After`.

Limits конфигурируются. Пиковая нагрузка или крупные payload могут потребовать меньшего
числа одновременно хранимых sessions; это нормальное применение backpressure.

После expiry рекомендуется сохранять краткоживущий tombstone/hash `payload_id`, чтобы
вернуть `410 Gone`, а не замаскировать ранее выданную маску как новый original. Tombstone
не содержит original, masked или mapping.

## Почему не Redis сразу

Сначала требуется измерить пропускную способность одного процесса и понять, является
ли state store bottleneck. Redis добавляет runtime-компонент, сериализацию raw PII,
сетевой failure mode и требования к защите данных. Он оправдан, если benchmark требует
нескольких workers/replicas или если state должен переживать restart.

Ramp-up и ограничение в 200 соединений делают single-process baseline более реалистичным;
Redis не является P0 без измеренного bottleneck.

## Ограничения

- Нельзя запускать несколько независимых Uvicorn workers.
- Restart теряет mapping и делает незавершённые пары невосстановимыми.
- ACTIVE TTL должен быть больше максимального интервала между парными запросами.
- COMPLETED TTL должен покрывать retry после потерянного demasking response.
- Approximate byte accounting требует тестов на ASCII и русский UTF-8 payload.

## Условия принятия или замены

ADR становится `Accepted`, когда предварительные TTL/limits подтверждены или изменены
baseline benchmark, зафиксированы atomic primitives и протестирован tombstone/expiry
response. Переход к shared store оформляется новым ADR с threat model,
шифрованием/transport security и измерениями latency/RPS.
