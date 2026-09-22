# Russian names dataset

Offline dataset used by the rule-based `NameDetector` to recognise Russian
full names (ФИО) without invoking the NER model.

## Sources

| File | Source | License |
|---|---|---|
| `raw/russian_surnames.txt` | [sorokinpf/russian_names](https://github.com/sorokinpf/russian_names) | — |
| `raw/russian_male_names.txt` | [sorokinpf/russian_names](https://github.com/sorokinpf/russian_names) | — |
| `raw/russian_female_names.txt` | [sorokinpf/russian_names](https://github.com/sorokinpf/russian_names) | — |
| `raw/midnames.jsonl` | [sorokinpf/russian_names](https://github.com/sorokinpf/russian_names) | — |
| `raw/raw_names.csv` | [mdanina/nen-imena-dataset](https://github.com/mdanina/nen-imena-dataset) | CC BY 4.0 |

## Attribution

The first-name data from `raw/raw_names.csv` is derived from the НЭН «Имена»
catalogue: **Данные: НЭН — сервис «Имена», [n-e-n.ru/imena](https://n-e-n.ru/imena/)**
(CC BY 4.0).

## Loading

`loader.py` reads the raw files at import time, lowercases and deduplicates
the values, and filters out non-Cyrillic noise. `russian_names.py` exposes the
consolidated `FIRST_NAMES`, `SURNAMES` and `PATRONYMICS` sets, falling back to
a small curated list when the raw files are absent.

## Refreshing

To refresh the dataset, re-download the files from the sources above into
`raw/` and re-run the tests.