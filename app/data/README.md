# Russian names dataset

Offline dataset used by the rule-based `NameDetector` to recognise Russian
full names (ФИО) without invoking the NER model.

## Files

| File | Purpose |
|---|---|
| `russian_names_full_99.py` | Primary dataset: Russian first names, patronymics and surnames with 99% coverage of the UCP2 golden individuals, grouped into HOT / MID / TAIL frequency tiers. |
| `russian_names.py` | Single public module: normalises the tiered sets to lowercase and exposes `FIRST_NAMES`, `SURNAMES`, `PATRONYMICS` plus the ordered tier tuples. Falls back to a minimal curated list when the full dataset is absent. |

## Tiered detection

The `NameDetector` checks HOT first, then MID, then TAIL, and only falls
through to NER when no tier matches. Confidence depends on the tier: HOT is
most confident, TAIL least.

## Refreshing

To refresh the dataset, replace `russian_names_full_99.py` with the updated
file and re-run the tests.