# ADR-0002: Хранилище состояния `/process`

- Статус: Accepted
- Дата: 2026-09-22

## Контекст

AlfaSonar вызывает `/process` дважды с одинаковым `payload_id`: сначала для masking,
затем для demasking. Между запросами необходимо сохранить original и masked.
Checker повторяет запросы, поэтому state transition должна быть идемпотентной.
Уточнённый профиль — до 200 параллельных соединений, последовательная пара
masking/demasking внутри соединения, разгон до 1000 RPS с удержанием достигнутого
уровня. В ответах организаторов также указан средний профиль около 330 RPS с пиками
до 1000; точные длительности ступеней не раскрыты.

## Решение

Использовать bounded in-memory TTL store в одном Uvicorn process:

- atomic get-or-create по `payload_id`;
- lifecycle `ACTIVE -> COMPLETED -> expired`;
- `ACTIVE`: original, masked, state, created/last-access timestamps, safe diagnostics;
- `COMPLETED`: original и masked сохраняются на короткий retry TTL;
- детерминированный ответ на retry;
- отдельные ACTIVE/COMPLETED TTL;
- max entries и approximate max bytes;
- controlled overload вместо неограниченного роста;
- raw session data не логируется.

Mapping в session **не хранится**: он существует только временно внутри
`PIIMasker.mask()` и удаляется после формирования masked. Demasking выполняется
возвратом сохранённого original, поскольку checker присылает точную строку masked.

Pending-координация: параллельный запрос с тем же `payload_id` и тем же payload
ожидает общий `asyncio.Future` (через `asyncio.shield`); другой payload получает
`409`. Capacity резервируется при создании PENDING и освобождается при
error/cancellation/expiry.

Значения, подтверждённые benchmark:

- `PROCESS_ACTIVE_TTL_SECONDS=900` — покрывает пятиминутный прогон и retries;
- `PROCESS_COMPLETED_TTL_SECONDS=120` — позволяет повторить demasking после потери ответа;
- `PROCESS_STORE_MAX_ENTRIES=25000` — соответствует средней нагрузке с запасом;
- `PROCESS_STORE_MAX_BYTES=536870912` (512 MiB) — защищает процесс от OOM;
- при исчерпании entry/byte limit возвращается `429` с `Retry-After`.

Limits конфигурируются. Пиковая нагрузка или крупные payload могут потребовать меньшего
числа одновременно хранимых sessions; это нормальное применение backpressure.

После expiry сохраняется краткоживущий tombstone/hash `payload_id`, чтобы вернуть
`410 Gone`, а не замаскировать ранее выданную маску как новый original. Tombstone
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

## Результаты benchmark

На локальном single-process baseline (rule-based + NameDetector, без NER) при 200
параллельных соединениях:

- masking: RPS ~269, latency mean 0.41s, p50 0.42s, p95 0.70s, p99 0.72s;
- 200 прямых вызовов `ProcessService.process` за ~0.03s (store/service не bottleneck).

Latency одного запроса укладывается в целевой 1s. NER остаётся выключенным для
`/process` по умолчанию и включается конфигурацией (`PROCESS_NER_ENABLED`).
