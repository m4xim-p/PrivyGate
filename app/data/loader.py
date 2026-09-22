"""Loader for the offline Russian names dataset.

Reads the raw dataset files downloaded from public sources and builds the
consolidated ``FIRST_NAMES``, ``SURNAMES`` and ``PATRONYMICS`` sets used by the
rule-based ``NameDetector``.

Sources:
- ``russian_surnames.txt``, ``russian_male_names.txt``,
  ``russian_female_names.txt``, ``midnames.jsonl`` from
  https://github.com/sorokinpf/russian_names (sorted Russian names/surnames
  wordlists).
- ``raw_names.csv`` from https://github.com/mdanina/nen-imena-dataset
  (1551 Russian first names, CC BY 4.0).

All values are lowercased and deduplicated.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "raw"

# A valid Russian word is composed only of Cyrillic letters (and ё).
_CYRILLIC_ONLY = re.compile(r"^[а-яё]+$")

# Common Russian surname endings.
_SURNAME_SUFFIXES = (
    "ов", "ев", "ёв", "ин", "ын", "ский", "ской", "цкий", "цкой",
    "ова", "ева", "ёва", "ина", "ына", "ская", "цкая",
)

# Common Russian patronymic endings.
_PATRONYMIC_SUFFIXES = (
    "ович", "евич", "овна", "евна", "ич", "ична", "инична",
)


def _is_valid_word(value: str, min_len: int, max_len: int) -> bool:
    return (
        min_len <= len(value) <= max_len
        and bool(_CYRILLIC_ONLY.match(value))
    )


def _read_lines(filename: str) -> set[str]:
    path = _DATA_DIR / filename
    if not path.exists():
        return set()
    return {
        line.strip().casefold()
        for line in path.open(encoding="utf-8")
        if line.strip()
    }


def _read_midnames(filename: str) -> set[str]:
    """Read patronymics from a JSON-lines file with a ``text`` field."""
    path = _DATA_DIR / filename
    if not path.exists():
        return set()
    values: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = record.get("text")
            if isinstance(text, str) and text.strip():
                values.add(text.strip().casefold())
    return values


def _read_csv_names(filename: str) -> set[str]:
    """Read first names from the NEN CSV (name column, first field)."""
    path = _DATA_DIR / filename
    if not path.exists():
        return set()
    values: set[str] = set()
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            name = line.split(",", 1)[0].strip()
            if name:
                values.add(name.casefold())
    return values


def load_first_names() -> set[str]:
    """Combine male, female and NEN first-name lists, keeping valid words."""
    names = _read_lines("russian_male_names.txt")
    names |= _read_lines("russian_female_names.txt")
    names |= _read_csv_names("raw_names.csv")
    return {
        name for name in names
        if _is_valid_word(name, 2, 15)
    }


def load_surnames() -> set[str]:
    """Load surnames, keeping only clean Cyrillic words with surname endings."""
    surnames = _read_lines("russian_surnames.txt")
    return {
        surname for surname in surnames
        if _is_valid_word(surname, 2, 20)
        and surname.endswith(_SURNAME_SUFFIXES)
    }


def load_patronymics() -> set[str]:
    """Load patronymics, keeping only clean Cyrillic words with -ич/-на endings."""
    patronymics = _read_midnames("midnames.jsonl")
    return {
        patronymic for patronymic in patronymics
        if _is_valid_word(patronymic, 3, 20)
        and patronymic.endswith(_PATRONYMIC_SUFFIXES)
    }