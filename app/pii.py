"""Request-scoped PII masking and boundary-safe streaming demasking."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol


EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+(?![\w-])"
)
PHONE_PATTERN = re.compile(
    r"(?<![0-9])(?:\+7|8)[\s-]*\(?[0-9]{3}\)?[\s-]*"
    r"[0-9]{3}[\s-]*[0-9]{2}[\s-]*[0-9]{2}(?![0-9])"
)
AMBIGUOUS_PHONE_PATTERN = re.compile(r"(?<![0-9])[0-9]{10}(?![0-9])")
PASSPORT_PATTERN = re.compile(
    r"(?<![0-9])(?P<series>(?P<region>[0-9]{2})[ -]?[0-9]{2})"
    r"[ \t]+(?:№[ \t]*)?(?P<number>[0-9]{6})(?![0-9])"
)
SNILS_PATTERN = re.compile(
    r"(?<![0-9])[0-9]{3}[- ]?[0-9]{3}[- ]?[0-9]{3}[ ]?[0-9]{2}(?![0-9])"
)
INN_PATTERN = re.compile(r"(?<![0-9])(?:[0-9]{12}|[0-9]{10})(?![0-9])")
CARD_PATTERN = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")

DEFAULT_MASKING_CONFIDENCE = 0.80


@dataclass(frozen=True, slots=True)
class PIIMatch:
    pii_type: str
    value: str
    start: int
    end: int
    confidence: float


@dataclass(frozen=True, slots=True)
class PIIDecision:
    """A safe-to-log masking decision that contains no PII value or text span."""

    pii_type: str
    confidence: float
    action: str


class PIIDetector(Protocol):
    def detect(self, text: str) -> list[PIIMatch]: ...


@dataclass(slots=True)
class PhoneDetectorConfig:
    """Tunable rule weights suitable for future external configuration."""

    context_window_chars: int = 48
    distance_decay_power: float = 1.0
    hard_context_boundaries: str = ",.;!?\n"
    soft_context_boundaries: str = ":—–"
    soft_boundary_multiplier: float = 0.75
    strong_phone_confidence: float = 0.99
    ambiguous_phone_confidence: float = 0.55
    positive_context_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "телефон": 0.35,
            "мобильный": 0.35,
            "позвони": 0.35,
            "номер телефона": 0.40,
            "связаться": 0.35,
        }
    )
    negative_context_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "заказ": 0.55,
            "тикет": 0.55,
            "артикул": 0.55,
            "договор": 0.55,
        }
    )


class EmailDetector:
    pii_type = "EMAIL"

    def __init__(self, confidence: float = 1.0) -> None:
        self._confidence = confidence

    def detect(self, text: str) -> list[PIIMatch]:
        return [
            PIIMatch(
                pii_type=self.pii_type,
                value=match.group(0),
                start=match.start(),
                end=match.end(),
                confidence=self._confidence,
            )
            for match in EMAIL_PATTERN.finditer(text)
        ]


class PhoneDetector:
    pii_type = "PHONE"

    def __init__(self, config: PhoneDetectorConfig | None = None) -> None:
        self.config = config if config is not None else PhoneDetectorConfig()

    def detect(self, text: str) -> list[PIIMatch]:
        matches = [
            self._to_match(match, self.config.strong_phone_confidence)
            for match in PHONE_PATTERN.finditer(text)
        ]
        matches.extend(
            self._ambiguous_match(text, match)
            for match in AMBIGUOUS_PHONE_PATTERN.finditer(text)
        )
        return matches

    def _ambiguous_match(self, text: str, match: re.Match[str]) -> PIIMatch:
        positive_adjustment = max(
            (
                self._context_influence(text, match, phrase, weight)
                for phrase, weight in self.config.positive_context_weights.items()
            ),
            default=0.0,
        )
        negative_adjustment = max(
            (
                self._context_influence(text, match, phrase, weight)
                for phrase, weight in self.config.negative_context_weights.items()
            ),
            default=0.0,
        )
        confidence = min(
            1.0,
            max(
                0.0,
                self.config.ambiguous_phone_confidence
                + positive_adjustment
                - negative_adjustment,
            ),
        )
        return self._to_match(match, confidence)

    def _context_influence(
        self,
        text: str,
        candidate: re.Match[str],
        phrase: str,
        configured_weight: float,
    ) -> float:
        strongest_influence = 0.0
        normalized_text = text.casefold()
        normalized_phrase = phrase.casefold()

        for marker in re.finditer(re.escape(normalized_phrase), normalized_text):
            if marker.end() <= candidate.start():
                distance = candidate.start() - marker.end()
                between = text[marker.end() : candidate.start()]
            elif marker.start() >= candidate.end():
                distance = marker.start() - candidate.end()
                between = text[candidate.end() : marker.start()]
            else:
                continue

            if distance > self.config.context_window_chars:
                continue

            boundary_multiplier = self._boundary_multiplier(between)
            if boundary_multiplier == 0.0:
                continue

            distance_ratio = distance / (self.config.context_window_chars + 1)
            distance_multiplier = (1.0 - distance_ratio) ** self.config.distance_decay_power
            influence = configured_weight * distance_multiplier * boundary_multiplier
            strongest_influence = max(strongest_influence, influence)

        return strongest_influence

    def _boundary_multiplier(self, between: str) -> float:
        if any(boundary in between for boundary in self.config.hard_context_boundaries):
            return 0.0
        if any(boundary in between for boundary in self.config.soft_context_boundaries):
            return self.config.soft_boundary_multiplier
        return 1.0

    def _to_match(self, match: re.Match[str], confidence: float) -> PIIMatch:
        return PIIMatch(
            pii_type=self.pii_type,
            value=match.group(0),
            start=match.start(),
            end=match.end(),
            confidence=confidence,
        )


class PassportDetector:
    """Detect supported, explicitly formatted Russian internal passports."""

    pii_type = "PASSPORT"

    def __init__(self, confidence: float = 0.99) -> None:
        self._confidence = confidence

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in PASSPORT_PATTERN.finditer(text):
            region = int(match.group("region"))
            passport_number = match.group("number")
            if region == 0 or passport_number == "000000":
                continue
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=self._confidence,
                )
            )
        return matches


class SNILSDetector:
    pii_type = "SNILS"

    def __init__(self, confidence: float = 1.0) -> None:
        self._confidence = confidence

    def detect(self, text: str) -> list[PIIMatch]:
        return [
            PIIMatch(
                pii_type=self.pii_type,
                value=match.group(0),
                start=match.start(),
                end=match.end(),
                confidence=self._confidence,
            )
            for match in SNILS_PATTERN.finditer(text)
            if self._has_valid_checksum(_digits(match.group(0)))
        ]

    @staticmethod
    def _has_valid_checksum(digits: str) -> bool:
        if len(digits) != 11:
            return False
        checksum_sum = sum(int(digit) * weight for digit, weight in zip(digits[:9], range(9, 0, -1)))
        if checksum_sum < 100:
            expected = checksum_sum
        elif checksum_sum in (100, 101):
            expected = 0
        else:
            expected = checksum_sum % 101
            if expected == 100:
                expected = 0
        return expected == int(digits[-2:])


class INNDetector:
    pii_type = "INN"
    _TEN_DIGIT_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    _TWELVE_DIGIT_FIRST_WEIGHTS = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    _TWELVE_DIGIT_SECOND_WEIGHTS = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)

    def __init__(self, confidence: float = 1.0) -> None:
        self._confidence = confidence

    def detect(self, text: str) -> list[PIIMatch]:
        return [
            PIIMatch(
                pii_type=self.pii_type,
                value=match.group(0),
                start=match.start(),
                end=match.end(),
                confidence=self._confidence,
            )
            for match in INN_PATTERN.finditer(text)
            if self._has_valid_checksum(match.group(0))
        ]

    @classmethod
    def _has_valid_checksum(cls, digits: str) -> bool:
        if len(digits) == 10:
            return cls._control_digit(digits[:9], cls._TEN_DIGIT_WEIGHTS) == int(digits[9])
        if len(digits) == 12:
            first_valid = (
                cls._control_digit(digits[:10], cls._TWELVE_DIGIT_FIRST_WEIGHTS)
                == int(digits[10])
            )
            second_valid = (
                cls._control_digit(digits[:11], cls._TWELVE_DIGIT_SECOND_WEIGHTS)
                == int(digits[11])
            )
            return first_valid and second_valid
        return False

    @staticmethod
    def _control_digit(digits: str, weights: Sequence[int]) -> int:
        return sum(int(digit) * weight for digit, weight in zip(digits, weights)) % 11 % 10


class CardDetector:
    pii_type = "CARD"

    def __init__(self, confidence: float = 1.0) -> None:
        self._confidence = confidence

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in CARD_PATTERN.finditer(text):
            digits = _digits(match.group(0))
            if not 13 <= len(digits) <= 19 or not self._passes_luhn(digits):
                continue
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=self._confidence,
                )
            )
        return matches

    @staticmethod
    def _passes_luhn(digits: str) -> bool:
        checksum = 0
        parity = len(digits) % 2
        for index, digit in enumerate(digits):
            value = int(digit)
            if index % 2 == parity:
                value *= 2
                if value > 9:
                    value -= 9
            checksum += value
        return checksum % 10 == 0


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isascii() and character.isdigit())


# Conventional CapWords aliases remain convenient for callers that prefer them.
SnilsDetector = SNILSDetector
InnDetector = INNDetector


def default_rule_detectors() -> tuple[PIIDetector, ...]:
    """Build the dependency-free detector set used when NER is disabled."""

    return (
        EmailDetector(),
        PhoneDetector(),
        PassportDetector(),
        SNILSDetector(),
        INNDetector(),
        CardDetector(),
    )


def resolve_overlapping_matches(matches: Sequence[PIIMatch]) -> list[PIIMatch]:
    """Prefer confidence, then longer spans, and return matches in text order."""

    unique: dict[tuple[str, int, int], PIIMatch] = {}
    for match in matches:
        key = (match.pii_type, match.start, match.end)
        previous = unique.get(key)
        if previous is None or match.confidence > previous.confidence:
            unique[key] = match

    ranked = sorted(
        unique.values(),
        key=lambda match: (
            -match.confidence,
            -(match.end - match.start),
            match.start,
            match.pii_type,
        ),
    )
    selected: list[PIIMatch] = []
    for candidate in ranked:
        if all(
            candidate.end <= existing.start or candidate.start >= existing.end
            for existing in selected
        ):
            selected.append(candidate)

    return sorted(selected, key=lambda match: (match.start, match.end, match.pii_type))


@dataclass(slots=True)
class MaskingResult:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)


class PIIMasker:
    """Collects a mapping whose lifetime is limited to one request."""

    def __init__(
        self,
        detectors: Sequence[PIIDetector] | None = None,
        min_confidence: float = DEFAULT_MASKING_CONFIDENCE,
    ) -> None:
        self.mapping: dict[str, str] = {}
        self.decisions: list[PIIDecision] = []
        self._counters: dict[str, int] = {}
        self._pii_types: set[str] = set()
        self._detectors = tuple(
            detectors
            if detectors is not None
            else default_rule_detectors()
        )
        self._min_confidence = min_confidence

    def mask(self, text: str) -> str:
        candidates = [
            match
            for detector in self._detectors
            for match in detector.detect(text)
        ]
        candidates.sort(key=lambda match: (match.start, match.end, match.pii_type))
        eligible = [
            match for match in candidates if match.confidence >= self._min_confidence
        ]
        matches = resolve_overlapping_matches(eligible)
        selected = set(matches)
        eligible_set = set(eligible)
        self.decisions.extend(
            PIIDecision(
                pii_type=match.pii_type,
                confidence=match.confidence,
                action=(
                    "masked"
                    if match in selected
                    else "overlap_rejected"
                    if match in eligible_set
                    else "below_threshold"
                ),
            )
            for match in candidates
        )
        if not matches:
            return text

        masked_parts: list[str] = []
        previous_end = 0
        for match in matches:
            masked_parts.append(text[previous_end : match.start])
            self._counters[match.pii_type] = self._counters.get(match.pii_type, 0) + 1
            placeholder = f"__PII_{match.pii_type}_{self._counters[match.pii_type]}__"
            self.mapping[placeholder] = match.value
            self._pii_types.add(match.pii_type)
            masked_parts.append(placeholder)
            previous_end = match.end

        masked_parts.append(text[previous_end:])
        return "".join(masked_parts)

    def result(self, text: str) -> MaskingResult:
        return MaskingResult(text=self.mask(text), mapping=dict(self.mapping))

    @property
    def pii_types(self) -> list[str]:
        return sorted(self._pii_types)


class StreamingDemasker:
    """Restores placeholders without assuming they fit inside one chunk."""

    def __init__(self, mapping: Mapping[str, str]) -> None:
        self._mapping = dict(mapping)
        self._placeholders = tuple(self._mapping)
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> str:
        output: list[str] = []

        while self._buffer:
            matches = [
                (position, placeholder)
                for placeholder in self._placeholders
                if (position := self._buffer.find(placeholder)) >= 0
            ]
            if matches:
                position, placeholder = min(matches, key=lambda item: item[0])
                output.append(self._buffer[:position])
                output.append(self._mapping[placeholder])
                self._buffer = self._buffer[position + len(placeholder) :]
                continue

            if final or not self._placeholders:
                output.append(self._buffer)
                self._buffer = ""
                break

            keep = self._possible_placeholder_prefix_length()
            if keep == len(self._buffer):
                break
            emit_until = len(self._buffer) - keep
            output.append(self._buffer[:emit_until])
            self._buffer = self._buffer[emit_until:]
            break

        return "".join(output)

    def _possible_placeholder_prefix_length(self) -> int:
        max_length = min(
            len(self._buffer),
            max((len(value) - 1 for value in self._placeholders), default=0),
        )
        for length in range(max_length, 0, -1):
            suffix = self._buffer[-length:]
            if any(placeholder.startswith(suffix) for placeholder in self._placeholders):
                return length
        return 0


def demask(text: str, mapping: Mapping[str, str]) -> str:
    demasker = StreamingDemasker(mapping)
    return demasker.feed(text) + demasker.flush()
