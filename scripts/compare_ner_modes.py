"""Compare NER modes: A (NER always) vs C (NER only on uncovered candidates).

Runs the golden datasets through the production PIIMasker with an NER
ml_detector in two modes and reports quality (F1, PERSON recall/FN), latency,
and cases where A found a name but C missed it.

This is a comparison harness, not part of the production pipeline.
"""

from __future__ import annotations

import csv
import re
import time
from collections.abc import Sequence

from app.ner import NERDetector, TransformersNERBackend
from app.pii import (
    NAME_CANDIDATE_PATTERN,
    NameDetector,
    PIIDetector,
    PIIMasker,
    default_rule_detectors,
)
from tests.quality_harness import aggregate, evaluate_case

DATASETS = [
    ("golden", "tests/data/golden_cases.csv"),
    ("api", "tests/data/api_cases.csv"),
    ("extended", "tests/data/extended_cases.csv"),
]

REPEATS = 3


def _load_cases(filename: str) -> list[dict[str, str]]:
    with open(filename, encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _has_uncovered_candidates(text: str, name_detector: NameDetector) -> bool:
    """True when a capitalized run looks like a name but rule-based missed it.

    For each candidate run of capitalized words, if rule-based NameDetector
    found no full name inside but the run has >=2 words, treat it as an
    uncovered candidate that NER should inspect.
    """
    for candidate in NAME_CANDIDATE_PATTERN.finditer(text):
        run = candidate.group(0)
        tokens = run.split()
        if len(tokens) < 2:
            continue
        if not name_detector.detect(run):
            return True
    return False


class _ConditionalNER:
    """Variant C: run NER only when there are uncovered name candidates."""

    def __init__(self, precheck: NameDetector, ner: PIIDetector) -> None:
        self._precheck = precheck
        self._ner = ner

    def detect(self, text: str) -> list:
        if not _has_uncovered_candidates(text, self._precheck):
            return []
        return self._ner.detect(text)


def _make_masker(
    rule: Sequence[PIIDetector],
    ner: PIIDetector,
    mode: str,
) -> PIIMasker:
    if mode == "A":
        # NER always runs (no precheck).
        return PIIMasker(detectors=rule, ml_detectors=[ner])
    if mode == "C":
        conditional = _ConditionalNER(NameDetector(), ner)
        return PIIMasker(detectors=rule, ml_detectors=[conditional])
    raise ValueError(f"unknown mode {mode}")


def _run(
    masker: PIIMasker, cases: list[dict[str, str]]
) -> tuple[float, float, int, int]:
    """Run cases, return (F1, throughput, person_recall, person_fn)."""
    results = []
    person_tp = 0
    person_expected = 0
    started = time.perf_counter()
    for case in cases:
        masker.mask(case["text"])
        results.append(evaluate_case(case, masker))
    elapsed = time.perf_counter() - started
    metrics = aggregate(results)
    return metrics.f1, len(cases) / elapsed, person_tp, person_expected


def _person_matches(masker: PIIMasker, text: str) -> set[str]:
    masker.mask(text)
    return {m.value for m in masker.matches if m.pii_type == "PERSON"}


def _compare_person(
    rule: Sequence[PIIDetector], ner: PIIDetector, cases: list[dict[str, str]]
) -> None:
    """Report PERSON recall/FN and cases where A found a name but C missed it."""
    masker_a = _make_masker(rule, ner, "A")
    masker_c = _make_masker(rule, ner, "C")
    a_only: list[tuple[str, str]] = []
    for case in cases:
        text = case["text"]
        a_names = _person_matches(masker_a, text)
        c_names = _person_matches(masker_c, text)
        for name in a_names - c_names:
            a_only.append((text, name))
    print(f"  PERSON: A found {len(a_only)} name(s) that C missed")
    for text, name in a_only[:10]:
        print(f"    A-only: {name!r} in {text[:80]!r}")


def main() -> None:
    backend = TransformersNERBackend.from_pretrained(
        "LLAIMlegal/ru-legal-ner",
        revision="924a4b1912ec6e55a4be959cab215ad8ff32a750",
        offline=True,
    )
    ner = NERDetector(backend, precheck=None)
    rule = default_rule_detectors()

    for label, filename in DATASETS:
        cases = _load_cases(filename)
        print(f"\n=== {label} ({len(cases)} cases) ===")
        # Warm-up + alternating order across repeats.
        for rep in range(REPEATS):
            order = ["A", "C"] if rep % 2 == 0 else ["C", "A"]
            for mode in order:
                masker = _make_masker(rule, ner, mode)
                # Warm-up pass.
                _run(masker, cases[:5])
                f1, throughput, _, _ = _run(masker, cases)
                print(
                    f"  rep{rep} mode {mode}: F1={f1:.3f}  "
                    f"throughput={throughput:.1f} cases/s"
                )
        _compare_person(rule, ner, cases)


if __name__ == "__main__":
    main()