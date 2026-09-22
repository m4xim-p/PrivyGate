"""Russian names dataset for rule-based ФИО detection.

The primary source is ``russian_names_full_99.py``: a unified set of Russian
first names, patronymics and surnames with 99% coverage of the UCP2 golden
individuals, grouped into frequency tiers (HOT / MID / TAIL).

This module normalises the tiered sets to lowercase and exposes them for the
``NameDetector``, which checks HOT first, then MID, then TAIL, and only falls
through to NER when no tier matches. When the full dataset file is absent, a
small curated fallback keeps the package importable.
"""

from __future__ import annotations

try:
    from app.data.russian_names_full_99 import (
        HOT_FIRST_NAMES,
        HOT_PATRONYMICS,
        HOT_SURNAMES,
        MID_FIRST_NAMES,
        MID_PATRONYMICS,
        MID_SURNAMES,
        TAIL_FIRST_NAMES,
        TAIL_PATRONYMICS,
        TAIL_SURNAMES,
    )
    _HAS_FULL = True
except ImportError:
    _HAS_FULL = False


def _norm(values: frozenset[str]) -> frozenset[str]:
    return frozenset(value.casefold() for value in values)


if _HAS_FULL:
    HOT_FIRST = _norm(HOT_FIRST_NAMES)
    MID_FIRST = _norm(MID_FIRST_NAMES)
    TAIL_FIRST = _norm(TAIL_FIRST_NAMES)

    HOT_SURNAME = _norm(HOT_SURNAMES)
    MID_SURNAME = _norm(MID_SURNAMES)
    TAIL_SURNAME = _norm(TAIL_SURNAMES)

    HOT_PATRONYMIC = _norm(HOT_PATRONYMICS)
    MID_PATRONYMIC = _norm(MID_PATRONYMICS)
    TAIL_PATRONYMIC = _norm(TAIL_PATRONYMICS)
else:
    # Minimal fallback so the package imports without the full dataset.
    HOT_FIRST = frozenset({"иван", "александр", "сергей", "мария", "анна"})
    MID_FIRST = frozenset()
    TAIL_FIRST = frozenset()

    HOT_SURNAME = frozenset({"иванов", "петров", "смирнов", "кузнецов"})
    MID_SURNAME = frozenset()
    TAIL_SURNAME = frozenset()

    HOT_PATRONYMIC = frozenset({"иванович", "ивановна", "петрович", "петровна"})
    MID_PATRONYMIC = frozenset()
    TAIL_PATRONYMIC = frozenset()

# Merged flat sets for callers that do not need tiering.
FIRST_NAMES = HOT_FIRST | MID_FIRST | TAIL_FIRST
SURNAMES = HOT_SURNAME | MID_SURNAME | TAIL_SURNAME
PATRONYMICS = HOT_PATRONYMIC | MID_PATRONYMIC | TAIL_PATRONYMIC

# Ordered tiers: HOT first, then MID, then TAIL.
FIRST_NAME_TIERS = (HOT_FIRST, MID_FIRST, TAIL_FIRST)
SURNAME_TIERS = (HOT_SURNAME, MID_SURNAME, TAIL_SURNAME)
PATRONYMIC_TIERS = (HOT_PATRONYMIC, MID_PATRONYMIC, TAIL_PATRONYMIC)