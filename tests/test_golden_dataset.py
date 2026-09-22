"""Golden dataset quality harness.

Runs the golden dataset in ``tests/data/golden_cases.csv`` through the default
rule-based masker and reports precision/recall/F1 plus FP/FN per category.

The dataset is the source of truth for P1 quality work. Known regressions are
reported (not silently ignored) so the team can track progress toward the 95%
target. This test does not fail on known gaps; it asserts the dataset is valid
and that the harness itself works.
"""

from __future__ import annotations

import csv
from pathlib import Path

from app.pii import PIIMasker

GOLDEN_CSV = Path(__file__).parent / "data" / "golden_cases.csv"


def _load_cases() -> list[dict[str, str]]:
    with GOLDEN_CSV.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_golden_dataset_is_valid() -> None:
    cases = _load_cases()
    assert cases, "golden dataset must not be empty"
    for case in cases:
        assert case["id"], "every case needs an id"
        assert case["category"], f"{case['id']}: missing category"
        assert case["kind"] in {"positive", "negative", "variant", "overlapping"}, (
            f"{case['id']}: unknown kind {case['kind']!r}"
        )
        assert case["expected_masked"] in {"true", "false"}, (
            f"{case['id']}: expected_masked must be true/false"
        )


def test_golden_dataset_has_both_kinds() -> None:
    cases = _load_cases()
    kinds = {case["kind"] for case in cases}
    assert "positive" in kinds and "negative" in kinds


def test_golden_dataset_covers_all_required_categories() -> None:
    cases = _load_cases()
    categories = {case["category"] for case in cases}
    required = {
        "PERSON",
        "DATE_OF_BIRTH",
        "BIRTH_PLACE",
        "PASSPORT",
        "CITIZENSHIP",
        "PASSPORT_AUTHORITY",
        "PASSPORT_UNIT_CODE",
        "PASSPORT_ISSUE_DATE",
        "DRIVING_LICENSE",
        "ADDRESS",
        "EMAIL",
        "PHONE",
        "INN",
        "CARD",
        "CVV",
        "PIN",
        "CARD_HOLDER",
    }
    missing = required - categories
    assert not missing, f"golden dataset missing categories: {sorted(missing)}"


def test_golden_dataset_quality_report() -> None:
    """Run the dataset and print a quality report (does not fail on gaps)."""
    cases = _load_cases()
    masker = PIIMasker()
    tp = tn = fp = fn = 0
    per_category: dict[str, dict[str, int]] = {}

    for case in cases:
        masked = masker.mask(case["text"])
        expected = case["expected_masked"] == "true"
        actual = masked != case["text"]

        stats = per_category.setdefault(
            case["category"], {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
        )
        if expected and actual:
            tp += 1
            stats["tp"] += 1
        elif expected and not actual:
            fn += 1
            stats["fn"] += 1
        elif not expected and actual:
            fp += 1
            stats["fp"] += 1
        else:
            tn += 1
            stats["tn"] += 1

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    report = [
        f"golden_dataset total={len(cases)} tp={tp} tn={tn} fp={fp} fn={fn}",
        f"precision={precision:.3f} recall={recall:.3f} f1={f1:.3f}",
    ]
    for category in sorted(per_category):
        s = per_category[category]
        cat_prec = s["tp"] / (s["tp"] + s["fp"]) if s["tp"] + s["fp"] else 0.0
        cat_rec = s["tp"] / (s["tp"] + s["fn"]) if s["tp"] + s["fn"] else 0.0
        report.append(
            f"  {category}: tp={s['tp']} fp={s['fp']} fn={s['fn']} "
            f"precision={cat_prec:.3f} recall={cat_rec:.3f}"
        )

    print("\n".join(report))