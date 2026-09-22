"""One-off generator: add expected_entities JSON column to dataset CSVs.

Computes canonical (type, start, end, value) for each expected span by locating
the span text in the case text and assigning a PII type. Type assignment for
overlapping cases uses an explicit per-case map derived from the note column.
This script is a development aid and is not part of the test suite.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "tests" / "data"


def find_offsets(text: str, span: str) -> list[tuple[int, int]]:
    idxs: list[tuple[int, int]] = []
    start = 0
    while True:
        i = text.find(span, start)
        if i < 0:
            break
        idxs.append((i, i + len(span)))
        start = i + 1
    return idxs


# Explicit type assignment for overlapping cases, keyed by case id.
# Each entry maps the expected span (by index) to a PII type.
_PASSPORT_BLOCK = [
    "PASSPORT",
    "PASSPORT_AUTHORITY",
    "PASSPORT_ISSUE_DATE",
    "PASSPORT_UNIT_CODE",
    "ADDRESS",
]
_PERSON_PASSPORT_BLOCK = [
    "PERSON",
    "DATE_OF_BIRTH",
    "PASSPORT",
    "PASSPORT_AUTHORITY",
    "PASSPORT_UNIT_CODE",
    "PASSPORT_ISSUE_DATE",
]
_PERSON_CITIZENSHIP_BLOCK = ["PERSON", "CITIZENSHIP", "PHONE", "ADDRESS"]
_PERSON_INN_BLOCK = ["PERSON", "INN", "PASSPORT", "ADDRESS"]
_PHONE_BLOCK = ["PHONE", "PERSON", "DATE_OF_BIRTH", "ADDRESS"]
_INN_BLOCK = ["INN", "KPP", "OGRN"]

OVERLAP_TYPES: dict[str, list[str]] = {
    # PASSPORT + authority + issue date + unit code + address
    "API001": _PASSPORT_BLOCK,
    "API007": _PASSPORT_BLOCK,
    "API008": _PASSPORT_BLOCK,
    "API013": _PASSPORT_BLOCK,
    "API016": _PASSPORT_BLOCK,
    "API025": _PASSPORT_BLOCK,
    "API027": _PASSPORT_BLOCK,
    "API030": _PASSPORT_BLOCK,
    "API032": _PASSPORT_BLOCK,
    "API053": _PASSPORT_BLOCK,
    # PERSON + DOB + passport + authority + unit code + issue date
    "API003": _PERSON_PASSPORT_BLOCK,
    "API005": _PERSON_PASSPORT_BLOCK,
    "API009": _PERSON_PASSPORT_BLOCK,
    "API014": _PERSON_PASSPORT_BLOCK,
    "API017": _PERSON_PASSPORT_BLOCK,
    "API018": _PERSON_PASSPORT_BLOCK,
    "API019": _PERSON_PASSPORT_BLOCK,
    "API020": _PERSON_PASSPORT_BLOCK,
    "API022": _PERSON_PASSPORT_BLOCK,
    "API026": _PERSON_PASSPORT_BLOCK,
    "API033": _PERSON_PASSPORT_BLOCK,
    "API040": _PERSON_PASSPORT_BLOCK,
    "API042": _PERSON_PASSPORT_BLOCK,
    "API044": _PERSON_PASSPORT_BLOCK,
    # PERSON + citizenship + phone + address
    "API010": _PERSON_CITIZENSHIP_BLOCK,
    "API012": _PERSON_CITIZENSHIP_BLOCK,
    "API015": _PERSON_CITIZENSHIP_BLOCK,
    "API021": _PERSON_CITIZENSHIP_BLOCK,
    "API024": _PERSON_CITIZENSHIP_BLOCK,
    "API038": _PERSON_CITIZENSHIP_BLOCK,
    "API045": _PERSON_CITIZENSHIP_BLOCK,
    "API050": _PERSON_CITIZENSHIP_BLOCK,
    # PERSON + INN + passport + address
    "API004": _PERSON_INN_BLOCK,
    "API011": _PERSON_INN_BLOCK,
    "API028": _PERSON_INN_BLOCK,
    "API036": _PERSON_INN_BLOCK,
    "API037": _PERSON_INN_BLOCK,
    "API041": _PERSON_INN_BLOCK,
    "API043": _PERSON_INN_BLOCK,
    "API047": _PERSON_INN_BLOCK,
    "API048": _PERSON_INN_BLOCK,
    "API052": _PERSON_INN_BLOCK,
    # PHONE + PERSON + DOB + address
    "API002": _PHONE_BLOCK,
    "API023": _PHONE_BLOCK,
    "API031": _PHONE_BLOCK,
    "API035": _PHONE_BLOCK,
    "API039": _PHONE_BLOCK,
    "API049": _PHONE_BLOCK,
    "API051": _PHONE_BLOCK,
    "API054": _PHONE_BLOCK,
    # PERSON + DOB + birth place + passport + authority + unit code + issue date
    "API006": [
        "PERSON",
        "DATE_OF_BIRTH",
        "BIRTH_PLACE",
        "PASSPORT",
        "PASSPORT_AUTHORITY",
        "PASSPORT_UNIT_CODE",
        "PASSPORT_ISSUE_DATE",
    ],
    "API029": [
        "PERSON",
        "EMAIL",
        "DATE_OF_BIRTH",
        "BIRTH_PLACE",
        "PASSPORT",
        "PASSPORT_ISSUE_DATE",
        "PASSPORT_AUTHORITY",
        "PASSPORT_UNIT_CODE",
    ],
    "API034": [
        "PERSON",
        "EMAIL",
        "DATE_OF_BIRTH",
        "BIRTH_PLACE",
        "PASSPORT",
        "PASSPORT_ISSUE_DATE",
        "PASSPORT_AUTHORITY",
        "PASSPORT_UNIT_CODE",
    ],
    "API046": ["PERSON", "DATE_OF_BIRTH", "BIRTH_PLACE", "EMAIL", "PHONE", "CITIZENSHIP"],
    "API100": ["PASSPORT", "DATE_OF_BIRTH", "BIRTH_PLACE"],
    # INN + KPP + OGRN
    "API055": _INN_BLOCK,
    "API060": ["INN", "OGRN", "ADDRESS"],
    "API062": ["INN", "KPP"],
    "API063": ["PERSON", "INN", "OGRN", "DATE_OF_BIRTH", "ADDRESS"],
    "API064": _INN_BLOCK,
    "API065": _INN_BLOCK,
    "API066": _INN_BLOCK,
    "API067": _INN_BLOCK,
    "API068": _INN_BLOCK,
    # CARD + holder + cvv + pin
    "API069": ["CARD", "CARD_HOLDER"],
    "API070": ["CARD", "CARD_HOLDER", "CVV", "PIN"],
    "API071": ["CARD", "CARD_HOLDER"],
    "API072": ["CARD", "CARD_HOLDER", "CVV"],
    "API073": ["CARD", "CARD", "PIN"],
    "API074": ["CARD", "CVV"],
    "API080": ["CARD", "CARD_HOLDER", "PIN"],
    # DRIVING_LICENSE + PERSON + DOB
    "API094": ["DRIVING_LICENSE", "PERSON", "DATE_OF_BIRTH"],
    "API095": ["DRIVING_LICENSE", "PERSON", "DATE_OF_BIRTH"],
    "API096": ["DRIVING_LICENSE", "PERSON", "DATE_OF_BIRTH"],
}

# Golden OVERLAPPING cases have no expected_span; define entities explicitly.
# Each entry maps a case id to a list of (type, value) pairs located in text.
GOLDEN_OVERLAP_ENTITIES: dict[str, list[tuple[str, str]]] = {
    "O01": [
        ("PERSON", "Иванов Иван Иванович"),
        ("PASSPORT", "4509 123456"),
        ("PHONE", "+7 999 123-45-67"),
    ],
    "O02": [
        ("PERSON", "Иванов Иван Иванович"),
        ("DATE_OF_BIRTH", "15.03.1990"),
        ("BIRTH_PLACE", "город Москва"),
    ],
    "O03": [
        ("CARD", "4111111111111111"),
        ("CVV", "123"),
        ("CARD_HOLDER", "IVAN PETROV"),
    ],
    "O04": [
        ("PASSPORT", "4509 123456"),
        ("PASSPORT_AUTHORITY", "ГУ МВД России по г. Москве"),
        ("PASSPORT_UNIT_CODE", "770-123"),
    ],
}


def build_entities(case: dict[str, str]) -> list[dict[str, object]]:
    text = case["text"]
    case_id = case["id"]
    if case_id in GOLDEN_OVERLAP_ENTITIES:
        entities: list[dict[str, object]] = []
        for pii_type, value in GOLDEN_OVERLAP_ENTITIES[case_id]:
            offsets = find_offsets(text, value)
            if len(offsets) != 1:
                raise ValueError(f"{case_id}: value {value!r} has {len(offsets)} offsets")
            start, end = offsets[0]
            entities.append({"type": pii_type, "start": start, "end": end, "value": value})
        return entities
    spans = [s.strip() for s in case.get("expected_span", "").split(";") if s.strip()]
    if not spans:
        return []
    if case_id in OVERLAP_TYPES:
        types = OVERLAP_TYPES[case_id]
        if len(types) != len(spans):
            raise ValueError(f"{case_id}: {len(types)} types vs {len(spans)} spans")
    else:
        types = [case["category"]] * len(spans)
    entities = []
    for span, pii_type in zip(spans, types, strict=True):
        offsets = find_offsets(text, span)
        if len(offsets) != 1:
            raise ValueError(f"{case_id}: span {span!r} has {len(offsets)} offsets")
        start, end = offsets[0]
        entities.append(
            {"type": pii_type, "start": start, "end": end, "value": span}
        )
    return entities


def process(filename: str) -> None:
    path = DATA_DIR / filename
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "expected_entities" not in fieldnames:
        fieldnames.append("expected_entities")
    for row in rows:
        row["expected_entities"] = json.dumps(
            build_entities(row), ensure_ascii=False, separators=(",", ":")
        )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{filename}: {len(rows)} rows updated")


if __name__ == "__main__":
    for name in ("golden_cases.csv", "api_cases.csv", "extended_cases.csv"):
        process(name)
