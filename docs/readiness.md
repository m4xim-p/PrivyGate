# Readiness PrivyGate

Оценка готовности решения к сдаче по критериям жюри (30 баллов) и критериям
финалистов. Документ для экзаменатора: показывает, что сделано, а что нет.

Дата фиксации: 2026-09-23.

## Что это

**PrivyGate** — модуль безопасности персональных данных (PII). Он встраивается в
цепочку «система-потребитель → Модуль → LLM» и защищает персональные данные при
обращении к языковым моделям:

1. **Идентифицирует** персональные данные (ФИО, паспорт, телефон, email, карты и т.д.).
2. **Маскирует** их перед отправкой в LLM.
3. **Демаскирует** ответ, если это разрешено политикой системы.

### Детекция персональных данных (17 обязательных категорий)

| Категория | Пример |
|---|---|
| ФИО | Иванов Иван Иванович |
| Дата рождения | 01.01.1990 |
| Место рождения | г. Москва |
| Паспорт РФ | 45 10 123456 |
| Гражданство | Российская Федерация |
| Орган выдачи | УФМС России |
| Код подразделения | 770-001 |
| Дата выдачи | 01.01.2010 |
| Водительское удостоверение | 77 01 123456 |
| Адрес | г. Москва, ул. Тестовая, д. 1 |
| Email | user@example.com |
| Телефон | +7 900 000 00 00 |
| ИНН | 770708389301 |
| Номер карты | 4000 0000 0000 0000 |
| CVV | 000 |
| PIN | 0000 |
| Имя держателя карты | IVANOV IVAN |

Дополнительно: **СНИЛС** (с checksum), **зарубежные паспорта**, **военный билет**.

**Гибридная детекция:** rule-based (regex + checksum) для структурированных типов
+ опциональный локальный NER для ФИО в сложных предложениях.

### Маскирование (3 режима)

| Режим | Пример |
|---|---|
| `typed_placeholder` (по умолчанию) | `__PII_PERSON_1__` |
| `synthetic` (синтетика) | `Иванов Иван Иванович` |
| `format_preserving` (сохранение формата) | `**** *****` |

### Демаскирование

- Восстановление исходного текста по mapping.
- Работает для `/process` (возврат сохранённого original) и для streaming-ответа LLM.

## 1. Сводка по 8 критериям (оценка ~25/30)

| # | Критерий | Макс | Оценка | Статус |
|---|---|---|---|---|
| 1 | Качество идентификации и маскирования ПДН | 6 | 4.5 | Все 17 категорий покрыты; golden F1 0.934; главный риск — api recall 0.918 (FN=90) |
| 2 | Корректность демаскирования | 3 | 3 | Boundary-safe demasking, round-trip 100%, только авторизованным системам |
| 3 | Точность отнесения к ПДН и устойчивость к вариациям | 4 | 3 | variants F1 0.950; ловушки (Пушкин, адрес банка) исправлены; FP на PHONE/ADDRESS |
| 4 | Гибкая настройка и расширяемость | 4 | 3.5 | Consumer policy, per-consumer PII types/masking_mode/demasking, custom_terms, config-driven |
| 5 | Производительность и SLA | 4 | 3.5 | ~1000 RPS на hold (999.5), p95 9ms; 100k токенов и RPS 2000 не подтверждены |
| 6 | Безопасность, логирование и метрики | 3 | 3 | `/metrics` есть; allowlist; fail-closed; логирование типов ПДН по запросу реализовано |
| 7 | Расширенные сценарии (доп. возможности) | 3 | 2 | synthetic, per-system masking_mode, контекстное (PIN+карта), custom_terms — 4 из 5 |
| 8 | Качество демо и презентации | 3 | 2 | README с демо-сценариями, схема, инструкция для жюри, ограничения и план развития есть; нет ZIP и evaluation URL |

## 2. Что сделано (по трекам roadmap)

### Трек A — политики потребителей и надёжность (критерий 3.4)
- [x] Consumer policy registry (`app/policy.py`).
- [x] Drift test между `process_api.yaml`, FastAPI schema и contract models.
- [x] Allowlist и optional API keys; evaluation `/process` без auth.
- [x] Per-consumer PII types и thresholds (enabled/excluded_pii_types, min_confidence).
- [x] Per-consumer masking mode: synthetic и format_preserving.
- [x] Per-consumer demasking permission (allow_demasking).
- [x] Fail-closed для `/process` при недоступности детектора.
- [x] Degradation policy `rule_only`.
- [x] Offline/pinned NER deployment (модель в образе, `local_files_only=True`).
- [x] Failure injection tests.

### Трек B — качество детекторов (критерии 3.1 и 3.3)
- [x] Строгий entity/span harness (`tests/quality_harness.py`).
- [x] Golden datasets (`tests/data/*.csv`), ratchet (`tests/test_golden_dataset.py`).
- [x] Исправление ловушек (исторические личности, корпоративные номера, PIN без карты).
- [x] Variants dataset (регистр, смешанные написания, опечатки).
- [ ] Поднять api recall (FN=90: PERSON 65, PASSPORT 25, INN 13) — главный пробел.
- [ ] Снизить FP: PHONE (горячая линия), ADDRESS (организации), EMAIL, DRIVING_LICENSE.

### Трек C — производительность и большие тексты (критерий 3.5)
- [x] Rate-controlled load test (`scripts/load_test_process.py`).
- [x] Baseline `/process` load test (`docs/benchmarks/load-test-baseline.md`).
- [x] k6 load test + real-time metrics collector.
- [x] CPU-профилирование (py-spy + `analyze_profile.py`).
- [x] `/metrics` endpoint (Prometheus text).
- [x] Оптимизация `ProcessStore` eviction (ADR-0006, min-heap): 78.77% → 0.70% CPU.
- [x] Tombstone retention (ADR-0006): bounded 50k, event loop delay 0ms.
- [x] Полный 5-минутный прогон: ~1000 HTTP RPS на hold (999.5), mask p95 9ms.
- [x] Подтверждение пика 1000 RPS на целевой конфигурации (полный 5-мин прогон, hold-фаза 999.5 HTTP RPS, mask p95 9ms, dropped_iterations ~0).
- [ ] Bounded NER concurrency и backpressure.
- [ ] Chunked/bounded обработка до 100 000 токенов.
- [ ] ONNX/quantization или shared store (только при подтверждённом bottleneck).

### Трек D — metrics endpoint и безопасность (критерий 3.6)
- [x] `/metrics` endpoint (Prometheus text): счётчики mask/demask/retry/429, store size, event loop delay, uptime.
- [x] Логирование выявленных типов ПДН по каждому запросу (`process_masked`/`request_started`/`upstream_rejected` логируют `pii_types` без raw PII).

### Трек E — дополнительные возможности (критерий 3.7)
- [x] Токенизация/детокенизация или замена синтетическими данными (`masking_mode: synthetic`).
- [x] Настройка вида маскирования/токенизации под систему (per-consumer `masking_mode`).
- [x] Контекстное маскирование по комбинации типов ПДН (PIN + номер карты, `require_card_for_pin`).
- [x] Custom terms маскирование по потребителю (ADR-0007).
- [ ] RPS 2000 при Latency ≤ 0.5 с (зависит от Трека C).
- [ ] Идентификация документов, удостоверяющих личность, кроме паспорта РФ (частично: DRIVING_LICENSE, SNILS). Дополнительно реализованы KPP/OGRN/SNILS, но они не входят в 17 обязательных категорий и не маскируются в `/process` по умолчанию (включаются через `enabled_pii_types`).

### Трек F — сдача и демонстрация (критерий 3.8 и артефакты)
- [x] Реальная LLM upstream (ADR-0008): `UpstreamClient` + `ModelRegistry`.
- [x] Merge-blocking Ruff/mypy/Bandit/pip-audit gate.
- [ ] Лёгкий ZIP без `.git`, environments, caches, models и build outputs.
- [ ] Доступный evaluation URL.
- [x] Инструкция для жюри по проверке (в `README.md`).
- [x] Схема архитектуры для жюри (`docs/architecture.md`).
- [x] Оценка готовности по критериям жюри (`docs/readiness.md`).
- [x] Архитектурная схема и результаты quality/load tests (в `docs/architecture.md`,
  `docs/benchmarks/quality-baseline.md`, `docs/benchmarks/load-test-baseline.md`).
- [x] Демо нормального, trap, policy и failure сценариев (в `README.md`).
- [x] Список известных ограничений и план развития (в `README.md`).

## 3. Качество (строгий entity/span harness)

| Dataset | Кейсов | Precision | Recall | F1 | Exact span acc | FN | FP |
|---|---:|---:|---:|---:|---:|---:|---:|
| golden | 180 | 0.907 | 0.963 | 0.934 | 0.975 | 1 | 6 |
| api | 342 | 0.944 | 0.918 | 0.931 | 0.960 | 90 | 33 |
| extended | 39 | 0.867 | 0.963 | 0.912 | 0.963 | 0 | 3 |
| variants | 62 | 0.971 | 0.931 | 0.950 | 0.971 | 3 | 0 |

Главный пробел — **api dataset FN=90** (PERSON 65, PASSPORT 25, INN 13). Целевые
95% не достигнуты. Это риск утечки ПДН в LLM (стоп-сигнал финалистов).

## 4. Нагрузка (критерий 3.5)

- **~1000 HTTP RPS на hold подтверждён** (999.5), mask p95 9ms, dropped_iterations ~0.
- Event loop delay 0ms, tombstones bounded 50k, RSS ~182 MiB.
- **100 000 токенов не подтверждено** — chunked/bounded обработка не реализована.
- **RPS 2000 не подтверждён** — требует подтверждения worker threads.

## 5. Доп. возможности (критерий 3.7, 2/3)

Реализовано: synthetic-замена, per-system masking_mode, контекстное маскирование
(PIN+карта), custom_terms. Не реализовано: RPS 2000, документы кроме паспорта
(частично).

## 6. Обязательные артефакты и критерии финалистов

**Не готово:** ZIP, evaluation URL.
**Готово:** README (включая демо-сценарии, ограничения и план развития), DEPLOY.md,
quality/load benchmarks, CI/CD, инструкция для жюри (в `README.md`), схема
архитектуры (`docs/architecture.md`), оценка готовности (этот документ).

**Критерии финалистов:** главный риск — **утечка ПДН в LLM** (стоп-сигнал).
api recall 0.918 означает ~9% пропусков; на живом демо со сложным текстом это
может провалить ключевую функцию. Приоритет №1 для финалистов — улучшение
api recall, затем артефакты сдачи.

## 7. Безопасность

- Raw PII, prompt и mapping **не логируются**.
- Логируются только типы ПДН, количество, latency, status.
- Локальный NER не отправляет данные во внешние сервисы.
- Fail-closed при недоступности детектора; rule_only degradation для разрешённых профилей.
- Allowlist для продуктового API; `/process` (автопроверка) без auth.

## 8. Примеры использования

### Система видит email, остальное скрыто

```json
{
  "consumer_id": "email-agent",
  "enabled": true,
  "excluded_pii_types": ["EMAIL"],
  "allow_demasking": true,
  "api_keys": ["CHANGE_ME_email_agent_api_key"]
}
```

### Система маскирует только паспорт

```json
{
  "consumer_id": "passport-only",
  "enabled": true,
  "enabled_pii_types": ["PASSPORT"],
  "allow_demasking": true,
  "api_keys": ["CHANGE_ME_passport_api_key"]
}
```

### Синтетическое маскирование

```json
{
  "consumer_id": "synthetic-agent",
  "enabled": true,
  "masking_mode": "synthetic",
  "allow_demasking": true,
  "api_keys": ["CHANGE_ME_synthetic_api_key"]
}
```

## 9. Как запустить

```bash
# Локально (NER включён по умолчанию)
uvicorn app.main:app --port 8000

# Без NER (только rule-based)
NER_ENABLED=false uvicorn app.main:app --port 8000

# Docker Compose
docker compose up --build
```

## 10. Приоритизация незакрытых задач

**Высокий приоритет (быстро, много баллов):**
1. Артефакты сдачи (Трек F, +1.5 балла, критерий 8) — ZIP, evaluation URL.
   Обязательные артефакты (инструкция для жюри, схема, демо-сценарии уже готовы).
2. api recall (Трек B, +1-1.5, критерии 1 и 3) — главный риск утечки ПДН.

**Средний приоритет:**
3. 100k токенов (Трек C, +0.5, критерий 5) — chunked/bounded обработка.

**Низкий приоритет:**
4. RPS 2000 (критерий 7, +1) — требует подтверждения worker threads.
5. Документы кроме паспорта РФ (критерий 7, +1) — частично закрыто.