"""Strict entity/span-based quality harness for PII detection.

This module evaluates the production PII pipeline (``PIIMasker``) against a
canonical annotation of expected entities. It does NOT re-implement detection,
overlap resolution or masking: it reads the final resolved ``PIIMatch`` list
that the masker already produced and compares it to the expected entities.

The harness is intentionally strict: a span error or a type error does NOT
count as a true positive. This prevents inflated precision/recall that the old
``masked != original`` heuristic produced.

Two metrics are kept separate:

* ``EntityMetrics`` — the internal strict entity/span metric keyed on
  ``(type, start, end)``. This is our own quality signal.
* ``organizer_approximation`` — a clearly-labelled approximation of the
  organizers' span-based scorer (normalized Levenshtein vs original, FN
  penalized more than FP, blanket masking penalized). It is NOT the official
  scorer and must never be presented as such.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.pii import PIIMasker, PIIMatch


@dataclass(frozen=True, slots=True)
class ExpectedEntity:
    """Canonical annotation of one expected PII entity."""

    type: str
    start: int
    end: int
    value: str


@dataclass(slots=True)
class CaseResult:
    """Classification of one dataset case against its expected entities."""

    case_id: str
    category: str
    tp: int = 0
    fn: int = 0
    fp: int = 0
    span_errors: int = 0
    type_errors: int = 0
    partial_leaks: int = 0
    overmasking: int = 0
    details: list[str] = field(default_factory=list)
    # Per-entity-type metrics so mixed/overlapping cases attribute their
    # entities to the real categories (PERSON, PASSPORT, ...) not OVERLAPPING.
    entity_types: dict[str, EntityMetrics] = field(default_factory=dict)


@dataclass(slots=True)
class EntityMetrics:
    """Strict entity/span metrics aggregated over a set of cases."""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    span_errors: int = 0
    type_errors: int = 0
    partial_leaks: int = 0
    overmasking: int = 0

    @property
    def precision(self) -> float:
        # Span/type errors are found but not exact, so they reduce precision.
        return _ratio(self.tp, self.tp + self.fp + self.span_errors + self.type_errors)

    @property
    def recall(self) -> float:
        # Span/type errors are expected but not exact, so they reduce recall.
        return _ratio(self.tp, self.tp + self.fn + self.span_errors + self.type_errors)

    @property
    def f1(self) -> float:
        return _f1(self.precision, self.recall)

    @property
    def exact_span_accuracy(self) -> float:
        denominator = self.tp + self.span_errors + self.type_errors
        return _ratio(self.tp, denominator)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def parse_expected_entities(raw: str) -> list[ExpectedEntity]:
    """Parse the ``expected_entities`` JSON column into canonical entities."""
    stripped = raw.strip()
    if not stripped or stripped == "[]":
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid expected_entities JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("expected_entities must be a JSON array")
    entities: list[ExpectedEntity] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each expected entity must be an object")
        try:
            entity = ExpectedEntity(
                type=str(item["type"]),
                start=int(item["start"]),
                end=int(item["end"]),
                value=str(item["value"]),
            )
        except KeyError as exc:
            raise ValueError(f"expected entity missing field {exc}") from exc
        if entity.start < 0 or entity.end < entity.start:
            raise ValueError(f"invalid span {entity.start}:{entity.end}")
        entities.append(entity)
    return entities


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def _classify_expected(
    expected: ExpectedEntity,
    actual: PIIMatch,
) -> str:
    """Classify one expected entity against one actual match.

    Returns one of: ``tp``, ``span_error``, ``type_error``.
    """
    if expected.type == actual.pii_type:
        if expected.start == actual.start and expected.end == actual.end:
            return "tp"
        return "span_error"
    return "type_error"


def _best_match(
    exp: ExpectedEntity,
    actual: list[PIIMatch],
    used_actual: set[int],
) -> tuple[int, str]:
    """Return ``(index, classification)`` of the best actual match for ``exp``.

    Prefers a true positive; otherwise the largest overlap. Returns
    ``(-1, "fn")`` when no unused actual match overlaps the expected entity.
    """
    best_index = -1
    best_overlap = 0
    best_class = "fn"
    for index, act in enumerate(actual):
        if index in used_actual:
            continue
        overlap = _overlap(exp.start, exp.end, act.start, act.end)
        if overlap <= 0:
            continue
        classification = _classify_expected(exp, act)
        priority = (classification == "tp", overlap)
        if best_index == -1 or priority > (best_class == "tp", best_overlap):
            best_index = index
            best_overlap = overlap
            best_class = classification
    return best_index, best_class


def evaluate_case(
    case: dict[str, str],
    masker: PIIMasker,
) -> CaseResult:
    """Evaluate a single dataset case against its expected entities.

    ``masker`` must already have been run on ``case["text"]`` (its ``matches``
    hold the final resolved spans). The case must carry an ``expected_entities``
    JSON column.
    """
    result = CaseResult(case_id=case["id"], category=case["category"])
    expected = parse_expected_entities(case.get("expected_entities", ""))
    actual = masker.matches

    if not expected:
        # Negative case: any actual match is a false positive. Attribute each
        # FP to the type of the found entity, not the case's category.
        result.fp = len(actual)
        for match in actual:
            _inc_entity_type(result, match.pii_type, fp=1)
            result.details.append(
                f"unexpected {match.pii_type}@{match.start}:{match.end}"
            )
        return result

    # Greedy best-overlap matching: each actual match is consumed once.
    used_actual: set[int] = set()
    for exp in expected:
        best_index, best_class = _best_match(exp, actual, used_actual)
        if best_index == -1:
            result.fn += 1
            _inc_entity_type(result, exp.type, fn=1)
            result.details.append(
                f"FN {exp.type}@{exp.start}:{exp.end} not found"
            )
            continue
        used_actual.add(best_index)
        act = actual[best_index]
        if best_class == "tp":
            result.tp += 1
            _inc_entity_type(result, exp.type, tp=1)
        elif best_class == "span_error":
            result.span_errors += 1
            _inc_entity_type(result, exp.type, span_errors=1)
            result.details.append(
                f"span_error {exp.type}@{exp.start}:{exp.end} vs "
                f"{act.pii_type}@{act.start}:{act.end}"
            )
        else:
            result.type_errors += 1
            _inc_entity_type(result, exp.type, type_errors=1)
            result.details.append(
                f"type_error expected {exp.type}@{exp.start}:{exp.end} got "
                f"{act.pii_type}@{act.start}:{act.end}"
            )

    # Any remaining actual match is a false positive.
    for index, act in enumerate(actual):
        if index not in used_actual:
            result.fp += 1
            _inc_entity_type(result, act.pii_type, fp=1)
            result.details.append(
                f"FP {act.pii_type}@{act.start}:{act.end}"
            )

    _detect_partial_leaks_and_overmasking(result, expected, actual, used_actual)
    return result


def _inc_entity_type(
    result: CaseResult,
    entity_type: str,
    *,
    tp: int = 0,
    fn: int = 0,
    fp: int = 0,
    span_errors: int = 0,
    type_errors: int = 0,
) -> None:
    """Increment per-entity-type metrics on a CaseResult."""
    metrics = result.entity_types.setdefault(entity_type, EntityMetrics())
    metrics.tp += tp
    metrics.fn += fn
    metrics.fp += fp
    metrics.span_errors += span_errors
    metrics.type_errors += type_errors


def _detect_partial_leaks_and_overmasking(
    result: CaseResult,
    expected: list[ExpectedEntity],
    actual: list[PIIMatch],
    used_actual: set[int],
) -> None:
    """Flag partial leaks (open chars inside an expected span) and overmasking.

    A partial leak occurs when part of an expected entity's span is not covered
    by any mask. Overmasking occurs when a mask covers characters beyond the
    expected entity's span. These are reported separately and do not affect the
    strict TP/FP/FN counts.
    """
    for exp in expected:
        covered = 0
        for act in actual:
            covered += _overlap(exp.start, exp.end, act.start, act.end)
        if covered < (exp.end - exp.start):
            result.partial_leaks += 1
            result.details.append(
                f"partial_leak {exp.type}@{exp.start}:{exp.end}"
            )
    for index in used_actual:
        act = actual[index]
        matched_expected = _expected_covering(expected, act)
        if matched_expected is None:
            continue
        if act.start < matched_expected.start or act.end > matched_expected.end:
            result.overmasking += 1
            result.details.append(
                f"overmask {act.pii_type}@{act.start}:{act.end} beyond "
                f"{matched_expected.type}@{matched_expected.start}:{matched_expected.end}"
            )


def _expected_covering(
    expected: list[ExpectedEntity],
    actual: PIIMatch,
) -> ExpectedEntity | None:
    for exp in expected:
        if _overlap(exp.start, exp.end, actual.start, actual.end) > 0:
            return exp
    return None


def aggregate(results: list[CaseResult]) -> EntityMetrics:
    """Aggregate per-case results into global entity metrics."""
    metrics = EntityMetrics()
    for result in results:
        metrics.tp += result.tp
        metrics.fp += result.fp
        metrics.fn += result.fn
        metrics.span_errors += result.span_errors
        metrics.type_errors += result.type_errors
        metrics.partial_leaks += result.partial_leaks
        metrics.overmasking += result.overmasking
    return metrics


def per_category_metrics(
    results: list[CaseResult],
) -> dict[str, EntityMetrics]:
    """Aggregate metrics grouped by entity type (not case category).

    Mixed/overlapping cases attribute their entities to the real categories
    (PERSON, PASSPORT, ...) rather than a synthetic OVERLAPPING bucket. Empty
    categories (no TP/FP/FN/span/type) are omitted.
    """
    grouped: dict[str, EntityMetrics] = {}
    for result in results:
        if result.entity_types:
            for entity_type, metrics in result.entity_types.items():
                target = grouped.setdefault(entity_type, EntityMetrics())
                target.tp += metrics.tp
                target.fn += metrics.fn
                target.fp += metrics.fp
                target.span_errors += metrics.span_errors
                target.type_errors += metrics.type_errors
        else:
            # Fallback for cases without per-entity breakdown.
            target = grouped.setdefault(result.category, EntityMetrics())
            target.tp += result.tp
            target.fn += result.fn
            target.fp += result.fp
            target.span_errors += result.span_errors
            target.type_errors += result.type_errors

    # Drop empty categories (e.g. a negative OVERLAPPING case with no entities).
    return {
        category: metrics
        for category, metrics in grouped.items()
        if metrics.tp or metrics.fp or metrics.fn or metrics.span_errors or metrics.type_errors
    }


def macro_f1(per_category: dict[str, EntityMetrics]) -> float:
    """Unweighted mean of per-category F1 (no invented category weights)."""
    if not per_category:
        return 0.0
    return sum(metrics.f1 for metrics in per_category.values()) / len(per_category)


def micro_f1(metrics: EntityMetrics) -> float:
    """Global F1 over all entities (naturally proportional to instance counts)."""
    return metrics.f1


def run_harness(
    cases: list[dict[str, str]],
) -> tuple[EntityMetrics, dict[str, EntityMetrics], list[CaseResult]]:
    """Run the strict harness over a list of cases.

    Returns ``(global_metrics, per_category, case_results)``.
    """
    results: list[CaseResult] = []
    for case in cases:
        masker = PIIMasker()
        masker.mask(case["text"])
        results.append(evaluate_case(case, masker))
    return aggregate(results), per_category_metrics(results), results


# ---------------------------------------------------------------------------
# Organizer scorer approximation (NOT the official scorer).
# ---------------------------------------------------------------------------


def organizer_approximation(
    cases: list[dict[str, str]],
) -> dict[str, Any]:
    """Approximate the organizers' span-based score.

    The organizers use a custom span-based metric relative to the original text
    with a normalized Levenshtein distance, penalize false negatives more than
    false positives, and penalize blanket masking and service-word capture. The
    exact formula and weights are not disclosed, so this is only a rough
    internal approximation and must not be presented as the official score.
    """
    total = 0.0
    count = 0
    for case in cases:
        masker = PIIMasker()
        masked = masker.mask(case["text"])
        expected = parse_expected_entities(case.get("expected_entities", ""))
        score = _case_approximation(case["text"], masked, expected)
        total += score
        count += 1
    return {
        "mean_score": total / count if count else 0.0,
        "note": "approximation only; not the official AlfaSonar scorer",
    }


def _case_approximation(
    original: str,
    masked: str,
    expected: list[ExpectedEntity],
) -> float:
    """Score one case: 1.0 minus normalized Levenshtein, with penalties."""
    if not expected:
        # Negative case: any masking is a false positive (penalized).
        return 0.0 if masked == original else 0.5
    if masked == original:
        # All expected entities leaked.
        return 0.0
    distance = _levenshtein(original, masked)
    normalized = distance / max(len(original), len(masked), 1)
    return max(0.0, 1.0 - normalized)


def _levenshtein(a: str, b: str) -> int:
    """Iterative Levenshtein distance (synthetic short strings only)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            substitute = previous[j - 1] + (char_a != char_b)
            current.append(min(insert, delete, substitute))
        previous = current
    return previous[-1]
