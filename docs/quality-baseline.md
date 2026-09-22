# Quality Baseline PrivyGate

Текущее качество detection/masking по golden datasets. Обновляется после каждого
изменения детекторов. Источник данных — `tests/data/*.csv`, harness —
`tests/test_golden_dataset.py`.

Дата фиксации: 2026-09-22.

## Сводка

| Dataset | Кейсов | Precision | Recall | F1 | Span errors |
|---|---:|---:|---:|---:|---:|
| golden | 177 | 0.893 | 0.971 | 0.931 | 0 |
| api | 116 | 0.960 | 1.000 | 0.980 | 87 |
| extended | 37 | 0.958 | 1.000 | 0.979 | 0 |

Recall высокий (0.97–1.00) — пропусков мало. Precision ниже (0.89–0.96) — есть
ложные срабатывания (FP). Это соответствует стратегии «пропуски штрафуются
сильнее лишних масок».

## Golden dataset (по категориям)

| Категория | tp | fp | fn | span | Precision | Recall |
|---|---:|---:|---:|---:|---:|---:|
| ADDRESS | 3 | 0 | 0 | 0 | 1.000 | 1.000 |
| BIRTH_PLACE | 4 | 1 | 0 | 0 | 0.800 | 1.000 |
| CARD | 4 | 0 | 0 | 0 | 1.000 | 1.000 |
| CARD_HOLDER | 4 | 0 | 0 | 0 | 1.000 | 1.000 |
| CITIZENSHIP | 3 | 0 | 0 | 0 | 1.000 | 1.000 |
| CVV | 3 | 0 | 0 | 0 | 1.000 | 1.000 |
| DATE_OF_BIRTH | 9 | 1 | 0 | 0 | 0.900 | 1.000 |
| DRIVING_LICENSE | 3 | 1 | 0 | 0 | 0.750 | 1.000 |
| EMAIL | 3 | 0 | 0 | 0 | 1.000 | 1.000 |
| INN | 1 | 0 | 1 | 0 | 1.000 | 0.500 |
| OVERLAPPING | 4 | 0 | 0 | 0 | 1.000 | 1.000 |
| PASSPORT | 3 | 0 | 1 | 0 | 1.000 | 0.750 |
| PASSPORT_AUTHORITY | 3 | 0 | 0 | 0 | 1.000 | 1.000 |
| PASSPORT_ISSUE_DATE | 2 | 0 | 0 | 0 | 1.000 | 1.000 |
| PASSPORT_UNIT_CODE | 2 | 0 | 0 | 0 | 1.000 | 1.000 |
| PERSON | 7 | 1 | 0 | 0 | 0.875 | 1.000 |
| PHONE | 6 | 4 | 0 | 0 | 0.600 | 1.000 |
| PIN | 3 | 0 | 0 | 0 | 1.000 | 1.000 |

## Ключевые проблемы

### Golden dataset
- **PHONE** (fp=4): горячая линия, служба поддержки, номера заказов маскируются.
- **INN** (fn=1): пропуск.
- **PASSPORT** (fn=1): пропуск.
- **PERSON** (fp=1), **DATE_OF_BIRTH** (fp=1), **DRIVING_LICENSE** (fp=1).

### API dataset (span_errors=87)
- **PERSON** (span=50): имена в сложных предложениях не полностью маскируются.
- **INN** (span=13), **CARD** (span=8), **PHONE** (span=7), **PASSPORT** (span=6).
- **ADDRESS**: решён двухстадийным детектором (точные spans, гранулы).

### Extended dataset
- **PERSON** (fn=3): пропуски ФИО.
- **PASSPORT** (fp=1): ложное срабатывание.

## Как воспроизвести

```bash
python -m pytest tests/test_golden_dataset.py -q -s
```

## Цель

Достичь F1 ≥ 0.95 на golden dataset при сохранении recall ≥ 0.95. Приоритет —
устранение пропусков (fn) и span-ошибок, затем снижение ложных срабатываний (fp).