"""Golden dataset strict entity/span quality harness.

Runs the golden datasets in ``tests/data/*.csv`` through the production
``PIIMasker`` and evaluates the final resolved ``PIIMatch`` spans against a
canonical annotation of expected entities (``expected_entities`` JSON column).

This harness is intentionally strict: a span error or a type error does NOT
count as a true positive. This prevents the inflated precision/recall that the
old ``masked != original`` heuristic produced.

Two metrics are kept separate:

* ``EntityMetrics`` — the internal strict entity/span metric keyed on
  ``(type, start, end)``. This is our own quality signal.
* ``organizer_approximation`` — a clearly-labelled approximation of the
  organizers' span-based scorer. It is NOT the official scorer.

Regression checks use a ratchet: they assert the current metrics do not drop
below the honestly-recomputed baseline. Thresholds are raised only after the
detectors genuinely improve.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tests.quality_harness import (
    macro_f1,
    micro_f1,
    organizer_approximation,
    parse_expected_entities,
    run_harness,
)

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

# Ratchet baseline: honestly-recomputed strict metrics on 2026-09-22.
# These are the current values; they must not regress. Raise them only after
# the detectors genuinely improve. Do NOT invent higher targets.
# Updated after harness fix (span/type errors reduce precision/recall, mixed
# cases attribute to real categories) and passport series/number split.
BASELINE = {
    "golden": {"f1": 0.908, "recall": 0.961, "exact_span_accuracy": 0.974},
    "api": {"f1": 0.746, "recall": 0.693, "exact_span_accuracy": 0.867},
    "extended": {"f1": 0.816, "recall": 0.870, "exact_span_accuracy": 0.870},
}

# Absolute floor on critical metrics regardless of dataset (recall-first).
MIN_RECALL = 0.50
MIN_EXACT_SPAN_ACCURACY = 0.70


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
def test_expected_entities_are_valid(filename: str, label: str) -> None:
    """Every expected_entities annotation must parse and match the text."""
    cases = _load_cases(filename)
    for case in cases:
        raw = case.get("expected_entities", "")
        entities = parse_expected_entities(raw)
        expected_masked = case["expected_masked"] == "true"
        if expected_masked:
            assert entities, (
                f"{label} {case['id']}: positive case must annotate entities"
            )
        for entity in entities:
            assert case["text"][entity.start : entity.end] == entity.value, (
                f"{label} {case['id']}: span {entity.start}:{entity.end} "
                f"does not match value {entity.value!r}"
            )


@pytest.mark.parametrize("filename,label", DATASETS)
def test_dataset_quality_report(filename: str, label: str) -> None:
    """Run the strict harness and print a quality report (does not fail)."""
    cases = _load_cases(filename)
    metrics, per_category, _ = run_harness(cases)
    report = [
        f"{label}_dataset total={len(cases)} tp={metrics.tp} fp={metrics.fp} "
        f"fn={metrics.fn} span_errors={metrics.span_errors} "
        f"type_errors={metrics.type_errors} partial_leaks={metrics.partial_leaks} "
        f"overmasking={metrics.overmasking}",
        f"precision={metrics.precision:.3f} recall={metrics.recall:.3f} "
        f"f1={metrics.f1:.3f} exact_span_accuracy={metrics.exact_span_accuracy:.3f}",
        f"macro_f1={macro_f1(per_category):.3f} micro_f1={micro_f1(metrics):.3f}",
    ]
    for category in sorted(per_category):
        m = per_category[category]
        report.append(
            f"  {category}: tp={m.tp} fp={m.fp} fn={m.fn} span={m.span_errors} "
            f"type={m.type_errors} leak={m.partial_leaks} over={m.overmasking} "
            f"precision={m.precision:.3f} recall={m.recall:.3f}"
        )
    print("\n".join(report))


@pytest.mark.parametrize("filename,label", DATASETS)
def test_no_regression_vs_baseline(filename: str, label: str) -> None:
    """Ratchet: metrics must not drop below the honest baseline."""
    cases = _load_cases(filename)
    metrics, _, _ = run_harness(cases)
    baseline = BASELINE[label]
    assert metrics.f1 >= baseline["f1"] - 0.01, (
        f"{label}: F1 {metrics.f1:.3f} dropped below baseline {baseline['f1']:.3f}"
    )
    assert metrics.recall >= baseline["recall"] - 0.01, (
        f"{label}: recall {metrics.recall:.3f} dropped below baseline "
        f"{baseline['recall']:.3f}"
    )
    assert metrics.exact_span_accuracy >= baseline["exact_span_accuracy"] - 0.01, (
        f"{label}: exact span accuracy {metrics.exact_span_accuracy:.3f} dropped "
        f"below baseline {baseline['exact_span_accuracy']:.3f}"
    )


@pytest.mark.parametrize("filename,label", DATASETS)
def test_critical_metric_floors(filename: str, label: str) -> None:
    """Absolute floors on critical metrics (recall-first, no blanket masking)."""
    cases = _load_cases(filename)
    metrics, _, _ = run_harness(cases)
    assert metrics.recall >= MIN_RECALL, (
        f"{label}: recall {metrics.recall:.3f} below floor {MIN_RECALL}"
    )
    assert metrics.exact_span_accuracy >= MIN_EXACT_SPAN_ACCURACY, (
        f"{label}: exact span accuracy {metrics.exact_span_accuracy:.3f} "
        f"below floor {MIN_EXACT_SPAN_ACCURACY}"
    )


def test_organizer_approximation_is_reported() -> None:
    """The organizer approximation is computed and printed (not asserted)."""
    for filename, label in DATASETS:
        cases = _load_cases(filename)
        approx = organizer_approximation(cases)
        print(f"{label}_dataset organizer_approximation={approx['mean_score']:.3f}")
        assert 0.0 <= approx["mean_score"] <= 1.0


def test_expected_entities_json_is_well_formed() -> None:
    """All expected_entities columns must be valid JSON arrays."""
    for filename, _ in DATASETS:
        cases = _load_cases(filename)
        for case in cases:
            raw = case.get("expected_entities", "")
            if not raw:
                continue
            payload = json.loads(raw)
            assert isinstance(payload, list), f"{case['id']}: not a JSON array"
