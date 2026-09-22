"""Golden dataset quality harness.

Runs the golden datasets in ``tests/data/*.csv`` through the default rule-based
masker and reports precision/recall/F1 plus FP/FN per category.

Datasets are the source of truth for P1 quality work. Known regressions are
reported (not silently ignored) so the team can track progress toward the 95%
target. These tests do not fail on known gaps; they assert each dataset is valid
and that the harness itself works.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.pii import PIIMasker

DATA_DIR = Path(__file__).parent / "data"
DATASETS = [
    ("golden_cases.csv", "golden"),
    ("api_cases.csv", "api"),
    ("extended_cases.csv", "extended"),
]

# Datasets expected to cover every required category as a primary category.
FULL_COVERAGE_DATASETS = {"golden", "api"}

REQUIRED_CATEGORIES = {
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


def _load_cases(filename: str) -> list[dict[str, str]]:
    with (DATA_DIR / filename).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.parametrize("filename,label", DATASETS)
def test_dataset_is_valid(filename: str, label: str) -> None:
    cases = _load_cases(filename)
    assert cases, f"{label} dataset must not be empty"
    for case in cases:
        assert case["id"], f"{label}: every case needs an id"
        assert case["category"], f"{label} {case['id']}: missing category"
        assert case["kind"] in {"positive", "negative", "variant", "overlapping"}, (
            f"{label} {case['id']}: unknown kind {case['kind']!r}"
        )
        assert case["expected_masked"] in {"true", "false"}, (
            f"{label} {case['id']}: expected_masked must be true/false"
        )


@pytest.mark.parametrize("filename,label", DATASETS)
def test_dataset_has_both_kinds(filename: str, label: str) -> None:
    cases = _load_cases(filename)
    kinds = {case["kind"] for case in cases}
    assert "positive" in kinds and "negative" in kinds, (
        f"{label} dataset must have both positive and negative cases"
    )


@pytest.mark.parametrize("filename,label", DATASETS)
def test_dataset_covers_all_required_categories(filename: str, label: str) -> None:
    if label not in FULL_COVERAGE_DATASETS:
        pytest.skip(f"{label} dataset is not expected to cover every category")
    cases = _load_cases(filename)
    categories = {case["category"] for case in cases}
    missing = REQUIRED_CATEGORIES - categories
    assert not missing, f"{label} dataset missing categories: {sorted(missing)}"


@pytest.mark.parametrize("filename,label", DATASETS)
def test_dataset_quality_report(filename: str, label: str) -> None:
    """Run the dataset and print a quality report (does not fail on gaps)."""
    cases = _load_cases(filename)
    masker = PIIMasker()
    tp = tn = fp = fn = 0
    span_errors = 0
    per_category: dict[str, dict[str, int]] = {}

    for case in cases:
        masked = masker.mask(case["text"])
        expected = case["expected_masked"] == "true"
        actual = masked != case["text"]

        stats = per_category.setdefault(
            case["category"], {"tp": 0, "tn": 0, "fp": 0, "fn": 0, "span": 0}
        )
        if expected and actual:
            tp += 1
            stats["tp"] += 1
            # Span accuracy: every expected span must be hidden in the mask.
            for span in _expected_spans(case):
                if span and span in masked:
                    span_errors += 1
                    stats["span"] += 1
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
        f"{label}_dataset total={len(cases)} tp={tp} tn={tn} fp={fp} fn={fn} "
        f"span_errors={span_errors}",
        f"precision={precision:.3f} recall={recall:.3f} f1={f1:.3f}",
    ]
    for category in sorted(per_category):
        s = per_category[category]
        cat_prec = s["tp"] / (s["tp"] + s["fp"]) if s["tp"] + s["fp"] else 0.0
        cat_rec = s["tp"] / (s["tp"] + s["fn"]) if s["tp"] + s["fn"] else 0.0
        report.append(
            f"  {category}: tp={s['tp']} fp={s['fp']} fn={s['fn']} "
            f"span={s['span']} precision={cat_prec:.3f} recall={cat_rec:.3f}"
        )

    print("\n".join(report))


def _expected_spans(case: dict[str, str]) -> list[str]:
    """Split the expected_span field into individual spans."""
    raw = case.get("expected_span", "").strip()
    if not raw or raw == "—":
        return []
    return [part.strip() for part in raw.split(";") if part.strip()]
