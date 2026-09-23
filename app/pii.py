"""Request-scoped PII masking and boundary-safe streaming demasking."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.address_detector import detect as _detect_addresses
from app.data.russian_names import FIRST_NAMES, PATRONYMICS, SURNAMES

EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+(?![\w-])"
)
PHONE_PATTERN = re.compile(
    r"(?<![0-9])(?:\+7|8)[\s-]*\(?[0-9]{3}\)?[\s-]*"
    r"[0-9]{3}[\s-]*[0-9]{2}[\s-]*[0-9]{2}(?![0-9])"
)
# Foreign phone numbers: +<country code> <area> <number> (e.g. +1, +44, +49).
FOREIGN_PHONE_PATTERN = re.compile(
    r"(?<![0-9])\+(?:[1-9][0-9]{0,2})[\s-]*\(?[0-9]{2,4}\)?[\s-]*"
    r"[0-9]{3,4}[\s-]*[0-9]{3,4}(?![0-9])"
)
AMBIGUOUS_PHONE_PATTERN = re.compile(r"(?<![0-9])[0-9]{10}(?![0-9])")
PASSPORT_PATTERN = re.compile(
    r"(?<![0-9])(?P<series>(?P<region>[0-9]{2})[ -]?[0-9]{2})"
    r"[ \t-]+(?:№[ \t]*)?(?P<number>[0-9]{6})(?![0-9])"
)
# Passport with separating words: "серия 4509 номер 123456", "серии 1234 № 123456".
# Captures the series and number as separate groups so the words "серия"/"номер"
# (and separators №/#/:/,/spaces) can stay open while the numeric parts are masked.
PASSPORT_SERIES_NUMBER_PATTERN = re.compile(
    r"(?<![0-9а-яёa-z])"
    r"(?:серия|серии|серией)[ \t]*[:#]?[ \t]*"
    r"(?P<series>(?P<region>[0-9]{2})[ -]?[0-9]{2})"
    r"[ \t]*[,]?[ \t]*"
    r"(?:(?:номер|номера|номером)[ \t]*[:#]?[ \t]*|[№#][ \t]*)?"
    r"(?P<number>[0-9]{6})"
    r"(?![0-9а-яёa-z])",
    flags=re.IGNORECASE,
)
# Russian foreign passport: 2 digits + 7 digits (e.g. 71 1234567).
FOREIGN_PASSPORT_PATTERN = re.compile(
    r"(?<![0-9])(?:[0-9]{2}[ \t]+[0-9]{7})(?![0-9])"
)
# Foreign citizen passport: 2 letters + 7 digits (e.g. AB1234567).
FOREIGN_ALPHA_PASSPORT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]{2}[0-9]{7})(?![0-9])"
)
# Russian military ID: 2 Cyrillic letters + 7 digits (e.g. АБ 1234567).
MILITARY_ID_PATTERN = re.compile(
    r"(?<![А-ЯЁа-яё0-9])(?:[А-ЯЁ]{2}[ \t\-]?(?:номер[ \t]*)?[0-9]{7})(?![0-9])"
)
SNILS_PATTERN = re.compile(
    r"(?<![0-9])[0-9]{3}[- ]?[0-9]{3}[- ]?[0-9]{3}[ ]?[0-9]{2}(?![0-9])"
)
INN_PATTERN = re.compile(r"(?<![0-9])(?:[0-9]{12}|[0-9]{10})(?![0-9])")
KPP_PATTERN = re.compile(r"(?<![0-9])[0-9]{9}(?![0-9])")
OGRN_PATTERN = re.compile(r"(?<![0-9])[0-9]{13}(?![0-9])")
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
        self._foreign_context = ContextConfig(
            positive_context_weights={
                "phone": 0.45,
                "номер": 0.35,
                "тел": 0.35,
                "телефон": 0.35,
                "мобильный": 0.35,
            },
        )
        # Corporate/help-desk numbers are not personal PII.
        self._corporate_markers = (
            "горячая линия",
            "служба поддержки",
            "контактный центр",
            "поддержка",
            "справочная",
            "колл-центр",
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches = [
            self._to_match(match, self.config.strong_phone_confidence)
            for match in PHONE_PATTERN.finditer(text)
            if not self._is_toll_free(match.group(0))
            and not self._is_corporate(text, match.start(), match.end())
        ]
        matches.extend(
            self._ambiguous_match(text, match)
            for match in AMBIGUOUS_PHONE_PATTERN.finditer(text)
        )
        matches.extend(self._foreign_phone_matches(text))
        return matches

    def _is_corporate(self, text: str, start: int, end: int) -> bool:
        """Return True when a phone number belongs to a corporate/help desk."""
        normalized = text.casefold()
        window_start = max(0, start - 60)
        window_end = min(len(text), end + 60)
        window = normalized[window_start:window_end]
        return any(marker in window for marker in self._corporate_markers)

    @staticmethod
    def _is_toll_free(value: str) -> bool:
        """Return True for toll-free 8-800 numbers (not personal PII)."""
        digits = _digits(value)
        return digits.startswith("8800")

    def _foreign_phone_matches(self, text: str) -> list[PIIMatch]:
        """Detect foreign phone numbers anchored by context markers."""
        matches: list[PIIMatch] = []
        for match in FOREIGN_PHONE_PATTERN.finditer(text):
            confidence = _context_confidence(
                text, match.start(), match.end(), self._foreign_context
            )
            if confidence < 0.80:
                continue
            matches.append(self._to_match(match, confidence))
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
    """Detect Russian internal, foreign and international passports."""

    pii_type = "PASSPORT"

    def __init__(self, confidence: float = 0.99) -> None:
        self._confidence = confidence
        self._driving_context = ContextConfig(
            positive_context_weights={
                "водительское удостоверение": 0.99,
                "водительского удостоверения": 0.99,
                "водительские права": 0.99,
                "водительских прав": 0.99,
                "права": 0.99,
                "прав": 0.99,
                "удостоверение": 0.99,
                "удостоверения": 0.99,
            },
        )
        self._foreign_context = ContextConfig(
            positive_context_weights={
                "загранпаспорт": 0.99,
                "загранпаспорта": 0.99,
                "passport": 0.99,
                "паспорт": 0.60,
                "паспорта": 0.60,
                "паспорт иностранного гражданина": 0.99,
                "паспорт гражданина": 0.99,
                "иностранный паспорт": 0.99,
                "иностранного паспорта": 0.99,
                "военный билет": 0.99,
                "военного билета": 0.99,
            },
        )
        # Alpha-numeric foreign passports (AB1234567) need a more specific
        # marker than a bare "паспорт"/"passport" to avoid false positives
        # (e.g. "Passport AB1234567" as a product code).
        self._foreign_alpha_context = ContextConfig(
            positive_context_weights={
                "паспорт иностранного гражданина": 0.99,
                "паспорт гражданина": 0.99,
                "иностранный паспорт": 0.99,
                "иностранного паспорта": 0.99,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in PASSPORT_PATTERN.finditer(text):
            region = int(match.group("region"))
            passport_number = match.group("number")
            if region == 0 or passport_number == "000000":
                continue
            confidence = self._confidence
            if _context_confidence(
                text, match.start(), match.end(), self._driving_context
            ) >= 0.99:
                confidence = 0.50
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        matches.extend(self._foreign_passport_matches(text))
        matches.extend(self._series_number_matches(text))
        return matches

    def _series_number_matches(self, text: str) -> list[PIIMatch]:
        """Detect passports written as 'серия XXXX номер XXXXXX'.

        Returns two separate matches (series and number) so the words
        "серия"/"номер" and separators stay open while the numeric parts are
        masked. The "серия ... номер ..." pattern itself is strong passport
        context, so no extra "паспорт" word is required.
        """
        matches: list[PIIMatch] = []
        for match in PASSPORT_SERIES_NUMBER_PATTERN.finditer(text):
            region = int(match.group("region"))
            passport_number = match.group("number")
            if region == 0 or passport_number == "000000":
                continue
            series_start = match.start("series")
            series_end = match.end("series")
            number_start = match.start("number")
            number_end = match.end("number")
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group("series"),
                    start=series_start,
                    end=series_end,
                    confidence=self._confidence,
                )
            )
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group("number"),
                    start=number_start,
                    end=number_end,
                    confidence=self._confidence,
                )
            )
        return matches

    def _foreign_passport_matches(self, text: str) -> list[PIIMatch]:
        """Detect foreign/international passports anchored by context markers."""
        matches: list[PIIMatch] = []
        for pattern in (
            FOREIGN_PASSPORT_PATTERN,
            FOREIGN_ALPHA_PASSPORT_PATTERN,
            MILITARY_ID_PATTERN,
        ):
            context = (
                self._foreign_alpha_context
                if pattern is FOREIGN_ALPHA_PASSPORT_PATTERN
                else self._foreign_context
            )
            for match in pattern.finditer(text):
                confidence = _context_confidence(
                    text, match.start(), match.end(), context
                )
                if confidence < 0.80:
                    continue
                matches.append(
                    PIIMatch(
                        pii_type=self.pii_type,
                        value=match.group(0),
                        start=match.start(),
                        end=match.end(),
                        confidence=confidence,
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
        checksum_sum = sum(
            int(digit) * weight
            for digit, weight in zip(digits[:9], range(9, 0, -1), strict=True)
        )
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
        return (
            sum(
                int(digit) * weight
                for digit, weight in zip(digits, weights, strict=True)
            )
            % 11
            % 10
        )


class KPPDetector:
    """Detect a KPP (tax registration reason code) anchored by context."""

    pii_type = "KPP"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "кпп": 0.45,
                "кпп ": 0.45,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in KPP_PATTERN.finditer(text):
            confidence = _context_confidence(
                text, match.start(), match.end(), self.config
            )
            if confidence < 0.80:
                continue
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        return matches


class OGRNDetector:
    """Detect an OGRN (primary state registration number) anchored by context."""

    pii_type = "OGRN"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "огрн": 0.45,
                "огрнип": 0.45,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in OGRN_PATTERN.finditer(text):
            confidence = _context_confidence(
                text, match.start(), match.end(), self.config
            )
            if confidence < 0.80:
                continue
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        return matches


class CardDetector:
    pii_type = "CARD"

    def __init__(self, confidence: float = 1.0) -> None:
        self._confidence = confidence

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in CARD_PATTERN.finditer(text):
            digits = _digits(match.group(0))
            if not _passes_luhn(digits):
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


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isascii() and character.isdigit())


def _passes_luhn(digits: str) -> bool:
    """Return True when a digit string passes the Luhn checksum."""
    if not 13 <= len(digits) <= 19:
        return False
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


# ---------------------------------------------------------------------------
# Shared context scoring for text-anchored detectors.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContextConfig:
    """Tunable rule weights for context-anchored PII detectors."""

    context_window_chars: int = 64
    distance_decay_power: float = 1.0
    hard_context_boundaries: str = ",.;!?\n"
    soft_context_boundaries: str = ":—–"
    soft_boundary_multiplier: float = 0.75
    strong_confidence: float = 0.99
    weak_confidence: float = 0.55
    positive_context_weights: Mapping[str, float] = field(default_factory=dict)
    negative_context_weights: Mapping[str, float] = field(default_factory=dict)


def _context_influence(
    text: str,
    start: int,
    end: int,
    phrase: str,
    configured_weight: float,
    config: ContextConfig,
) -> float:
    """Score how strongly a context phrase near [start, end) supports a match."""
    strongest_influence = 0.0
    normalized_text = text.casefold()
    normalized_phrase = phrase.casefold()

    for marker in re.finditer(re.escape(normalized_phrase), normalized_text):
        if marker.end() <= start:
            distance = start - marker.end()
            between = text[marker.end() : start]
        elif marker.start() >= end:
            distance = marker.start() - end
            between = text[end : marker.start()]
        else:
            continue

        if distance > config.context_window_chars:
            continue

        boundary_multiplier = _boundary_multiplier(between, config)
        if boundary_multiplier == 0.0:
            continue

        distance_ratio = distance / (config.context_window_chars + 1)
        distance_multiplier = (1.0 - distance_ratio) ** config.distance_decay_power
        influence = configured_weight * distance_multiplier * boundary_multiplier
        strongest_influence = max(strongest_influence, influence)

    return strongest_influence


def _boundary_multiplier(between: str, config: ContextConfig) -> float:
    # A period inside an abbreviation (г., ул.) is not a hard boundary.
    if any(boundary in between for boundary in config.hard_context_boundaries):
        # Re-check: ignore periods that are part of abbreviations.
        cleaned = _strip_abbreviation_periods(between)
        if any(boundary in cleaned for boundary in config.hard_context_boundaries):
            return 0.0
    if any(boundary in between for boundary in config.soft_context_boundaries):
        return config.soft_boundary_multiplier
    return 1.0


def _strip_abbreviation_periods(text: str) -> str:
    """Remove periods that are part of abbreviations like 'г.' or 'ул.'."""
    result = []
    i = 0
    while i < len(text):
        if text[i] == "." and i > 0 and text[i - 1].isalpha():
            # Period preceded by a letter: abbreviation like 'г.' or 'ул.'.
            i += 1
            continue
        result.append(text[i])
        i += 1
    return "".join(result)


def _is_abbreviation_continuation(text: str, period_index: int) -> bool:
    """Return True when a period starts an abbreviation like 'г.' or 'ул.'."""
    next_index = period_index + 1
    while next_index < len(text) and text[next_index] in " \t":
        next_index += 1
    if next_index >= len(text):
        return False
    return text[next_index].isalpha()


def _is_date_start(text: str, index: int) -> bool:
    """Return True when a date (ISO or numeric) starts at ``index``."""
    if index + 9 >= len(text):
        return False
    # ISO YYYY-MM-DD
    if (
        text[index : index + 4].isdigit()
        and text[index + 4] == "-"
        and text[index + 5 : index + 7].isdigit()
        and text[index + 7] == "-"
        and text[index + 8 : index + 10].isdigit()
    ):
        return True
    # Numeric DD.MM.YYYY / DD.MM.YY
    return (
        text[index : index + 2].isdigit()
        and text[index + 2] == "."
        and text[index + 3 : index + 5].isdigit()
        and text[index + 5] == "."
    )


def _is_sentence_end(text: str, period_index: int) -> bool:
    """Return True when a period ends a sentence (uppercase or end of text)."""
    next_index = period_index + 1
    while next_index < len(text) and text[next_index] in " \t":
        next_index += 1
    if next_index >= len(text):
        return True
    return text[next_index].isupper()


def _context_confidence(
    text: str,
    start: int,
    end: int,
    config: ContextConfig,
) -> float:
    """Combine positive and negative context into a bounded confidence score."""
    positive_adjustment = max(
        (
            _context_influence(text, start, end, phrase, weight, config)
            for phrase, weight in config.positive_context_weights.items()
        ),
        default=0.0,
    )
    negative_adjustment = max(
        (
            _context_influence(text, start, end, phrase, weight, config)
            for phrase, weight in config.negative_context_weights.items()
        ),
        default=0.0,
    )
    return min(
        1.0,
        max(
            0.0,
            config.weak_confidence + positive_adjustment - negative_adjustment,
        ),
    )


# ---------------------------------------------------------------------------
# Date of birth.
# ---------------------------------------------------------------------------

# Numeric dates: dd.mm.yyyy, mm.dd.yyyy, yyyy.mm.dd, yyyy.dd.mm with any of
# . / - separators. The exact field order is validated against the calendar.
DATE_NUMERIC_PATTERN = re.compile(
    r"(?<![0-9])(?P<first>[0-9]{1,4})[./-](?P<second>[0-9]{1,2})[./-]"
    r"(?P<third>[0-9]{2,4})(?![0-9])"
)

# Textual dates: "пятнадцатого марта 1990 года", "15 марта 1990 г."
RUSSIAN_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
}
RUSSIAN_DAY_WORDS = {
    "первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "четвёртого": 4,
    "пятого": 5, "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9,
    "десятого": 10, "одиннадцатого": 11, "двенадцатого": 12, "тринадцатого": 13,
    "четырнадцатого": 14, "пятнадцатого": 15, "шестнадцатого": 16,
    "семнадцатого": 17, "восемнадцатого": 18, "девятнадцатого": 19,
    "двадцатого": 20, "двадцать первого": 21, "двадцать второго": 22,
    "двадцать третьего": 23, "двадцать четвертого": 24, "двадцать четвёртого": 24,
    "двадцать пятого": 25, "двадцать шестого": 26, "двадцать седьмого": 27,
    "двадцать восьмого": 28, "двадцать девятого": 29, "тридцатого": 30,
    "тридцать первого": 31,
}
DATE_TEXT_PATTERN = re.compile(
    r"(?<![а-яёa-z0-9])"
    r"(?P<day>(?:двадцать\s+)?(?:первого|второго|третьего|четвертого|четвёртого|"
    r"пятого|шестого|седьмого|восьмого|девятого|десятого|одиннадцатого|"
    r"двенадцатого|тринадцатого|четырнадцатого|пятнадцатого|шестнадцатого|"
    r"семнадцатого|восемнадцатого|девятнадцатого|двадцатого|тридцатого|"
    r"тридцать\s+первого))"
    r"\s+"
    r"(?P<month>января|февраля|марта|апреля|мая|июня|июля|августа|сентября|"
    r"октября|ноября|декабря)"
    r"(?:\s+(?P<year>[0-9]{4}))?"
    r"(?:\s+(?:года|г\.))?"
    r"(?![а-яёa-z0-9])",
    flags=re.IGNORECASE,
)

# Numeric day + month word: "11 декабря 2005 г."
NUMERIC_DAY_MONTH_PATTERN = re.compile(
    r"(?<![а-яёa-z0-9])"
    r"(?P<day>[0-9]{1,2})\s+"
    r"(?P<month>января|февраля|марта|апреля|мая|июня|июля|августа|сентября|"
    r"октября|ноября|декабря)"
    r"(?:\s+(?P<year>[0-9]{4}))?"
    r"(?:\s+(?:года|г\.))?"
    r"(?![а-яёa-z0-9])",
    flags=re.IGNORECASE,
)

# English textual dates: "15 March 1990", "March 15, 1990".
ENGLISH_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
ENGLISH_DATE_PATTERN = re.compile(
    r"(?<![a-z0-9])"
    r"(?:(?P<day>[0-9]{1,2})\s+(?P<month1>january|february|march|april|may|june|"
    r"july|august|september|october|november|december)"
    r"(?:,?\s+(?P<year1>[0-9]{4}))?"
    r"|(?P<month2>january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+(?P<day2>[0-9]{1,2}),?\s+(?P<year2>[0-9]{4}))"
    r"(?![a-z0-9])",
    flags=re.IGNORECASE,
)


def _is_valid_calendar_date(day: int, month: int, year: int) -> bool:
    if not (1 <= month <= 12):
        return False
    if year < 1900 or year > 2100:
        return False
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 1 <= day <= 31
    if month in (4, 6, 9, 11):
        return 1 <= day <= 30
    leap = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
    return 1 <= day <= (29 if leap else 28)


def _expand_year(year: int) -> int | None:
    """Expand a 2-digit year (78 -> 1978, 05 -> 2005) or pass through 4-digit."""
    if 1900 <= year <= 2100:
        return year
    if 0 <= year <= 99:
        return 1900 + year if year >= 30 else 2000 + year
    return None


class DateOfBirthDetector:
    """Detect dates of birth in numeric and textual Russian formats."""

    pii_type = "DATE_OF_BIRTH"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "дата рождения": 0.40,
                "родился": 0.35,
                "родилась": 0.35,
                "день рождения": 0.35,
                "date of birth": 0.45,
                "born on": 0.40,
                "born": 0.35,
            },
            negative_context_weights={
                "срок действия": 0.55,
                "действует до": 0.55,
                "истекает": 0.55,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in DATE_NUMERIC_PATTERN.finditer(text):
            parsed = self._parse_numeric(match)
            if parsed is None:
                continue
            day, month, year = parsed
            confidence = self._confidence(text, match.start(), match.end())
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        for match in DATE_TEXT_PATTERN.finditer(text):
            text_day = RUSSIAN_DAY_WORDS.get(match.group("day").casefold())
            text_month = RUSSIAN_MONTHS.get(match.group("month").casefold())
            year_text = match.group("year")
            text_year = int(year_text) if year_text else None
            if text_day is None or text_month is None:
                continue
            if text_year is not None and not _is_valid_calendar_date(
                text_day, text_month, text_year
            ):
                continue
            confidence = self._confidence(text, match.start(), match.end())
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        matches.extend(self._english_date_matches(text))
        return matches

    def _english_date_matches(self, text: str) -> list[PIIMatch]:
        """Detect English textual dates anchored by birth context."""
        matches: list[PIIMatch] = []
        for match in ENGLISH_DATE_PATTERN.finditer(text):
            day_text = match.group("day") or match.group("day2")
            month_text = match.group("month1") or match.group("month2")
            year_text = match.group("year1") or match.group("year2")
            if day_text is None or month_text is None:
                continue
            day = int(day_text)
            month = ENGLISH_MONTHS.get(month_text.casefold())
            year = int(year_text) if year_text else None
            if month is None:
                continue
            if year is not None and not _is_valid_calendar_date(day, month, year):
                continue
            confidence = self._confidence(text, match.start(), match.end())
            if confidence < 0.80:
                continue
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        return matches

    @staticmethod
    def _parse_numeric(match: re.Match[str]) -> tuple[int, int, int] | None:
        first = int(match.group("first"))
        second = int(match.group("second"))
        third = int(match.group("third"))

        # yyyy.mm.dd / yyyy.dd.mm
        if first >= 1900:
            if _is_valid_calendar_date(second, third, first):
                return second, third, first
            if _is_valid_calendar_date(third, second, first):
                return third, second, first
            return None

        # dd.mm.yyyy / mm.dd.yyyy. Support 2-digit years (78 -> 1978).
        year = _expand_year(third)
        if year is None:
            return None
        if _is_valid_calendar_date(first, second, year):
            return first, second, year
        if _is_valid_calendar_date(second, first, year):
            return second, first, year
        return None

    def _confidence(self, text: str, start: int, end: int) -> float:
        return _context_confidence(text, start, end, self.config)


# ---------------------------------------------------------------------------
# Birth place.
# ---------------------------------------------------------------------------


class BirthPlaceDetector:
    """Detect a place of birth anchored by explicit context markers."""

    pii_type = "BIRTH_PLACE"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "место рождения": 0.40,
                "родился в": 0.35,
                "родилась в": 0.35,
                "родился в городе": 0.40,
                "родилась в городе": 0.40,
                "родился": 0.40,
                "родилась": 0.40,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        normalized = text.casefold()
        for phrase in self.config.positive_context_weights:
            for marker in re.finditer(re.escape(phrase.casefold()), normalized):
                start = self._trim_start(text, marker.end())
                start = self._skip_place_markers(text, start)
                # For bare "родился"/"родилась", skip a date and find "в".
                if phrase in ("родился", "родилась"):
                    place_start = self._skip_to_place(text, start)
                    if place_start is None:
                        continue
                    start = place_start
                end = self._place_end(text, start)
                if end <= start:
                    continue
                confidence = _context_confidence(
                    text, start, end, self.config
                )
                matches.append(
                    PIIMatch(
                        pii_type=self.pii_type,
                        value=text[start:end],
                        start=start,
                        end=end,
                        confidence=confidence,
                    )
                )
        return matches

    @staticmethod
    def _skip_to_place(text: str, start: int) -> int | None:
        """Skip a date/suffix after 'родился' and return the position after 'в'.

        Returns None when no 'в' (place marker) is found, so a bare date after
        'родился' is not misclassified as a birth place.
        """
        lowered = text.casefold()
        idx = start
        while idx < len(text):
            if lowered.startswith("в ", idx):
                return idx + 2
            if text[idx].isalpha():
                idx += 1
                continue
            idx += 1
        return None

    @staticmethod
    def _skip_place_markers(text: str, start: int) -> int:
        """Skip service words like 'город', 'городе', 'г.' that are not PII."""
        lowered = text.casefold()
        for marker in ("город ", "городе ", "г. ", "г "):
            if lowered.startswith(marker, start):
                return start + len(marker)
        return start

    @staticmethod
    def _trim_start(text: str, start: int) -> int:
        while start < len(text) and text[start] in " \t":
            start += 1
        return start

    @staticmethod
    def _place_end(text: str, start: int) -> int:
        end = start
        while end < len(text):
            character = text[end]
            if character in ",;!?\n":
                break
            if character == ".":
                # Keep abbreviations like "г." and "ул." inside the value.
                if _is_abbreviation_continuation(text, end):
                    end += 1
                    continue
                break
            end += 1
        return end


# ---------------------------------------------------------------------------
# Citizenship.
# ---------------------------------------------------------------------------


class CitizenshipDetector:
    """Detect citizenship anchored by explicit context markers."""

    pii_type = "CITIZENSHIP"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "гражданство": 0.40,
                "гражданин": 0.35,
                "гражданка": 0.35,
                "citizenship": 0.45,
                "citizen": 0.40,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        normalized = text.casefold()
        for phrase in self.config.positive_context_weights:
            pattern = re.compile(rf"\b{re.escape(phrase.casefold())}\b")
            for marker in pattern.finditer(normalized):
                start = self._trim_start(text, marker.end())
                end = self._value_end(text, start)
                if end <= start:
                    continue
                confidence = _context_confidence(text, start, end, self.config)
                matches.append(
                    PIIMatch(
                        pii_type=self.pii_type,
                        value=text[start:end],
                        start=start,
                        end=end,
                        confidence=confidence,
                    )
                )
        return matches

    @staticmethod
    def _trim_start(text: str, start: int) -> int:
        while start < len(text) and text[start] in " \t":
            start += 1
        return start

    @staticmethod
    def _value_end(text: str, start: int) -> int:
        end = start
        while end < len(text):
            character = text[end]
            if character in ",;!?\n":
                break
            if character == ".":
                # A period ends the citizenship value unless it is an
                # abbreviation inside the value (e.g. "г. Москва").
                if _is_abbreviation_continuation(text, end) \
                        and not _is_sentence_end(text, end):
                    end += 1
                    continue
                break
            end += 1
        return end


# ---------------------------------------------------------------------------
# Passport authority and unit code.
# ---------------------------------------------------------------------------

PASSPORT_UNIT_CODE_PATTERN = re.compile(
    r"(?<![0-9])(?P<code>[0-9]{3}-[0-9]{3})(?![0-9])"
)


class PassportAuthorityDetector:
    """Detect the authority that issued a passport."""

    pii_type = "PASSPORT_AUTHORITY"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "кем выдан": 0.40,
                "выдан": 0.30,
                "орган, выдавший": 0.40,
                "орган выдавший": 0.40,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        normalized = text.casefold()
        for phrase in self.config.positive_context_weights:
            for marker in re.finditer(re.escape(phrase.casefold()), normalized):
                start = self._trim_start(text, marker.end())
                end = self._value_end(text, start)
                if end <= start:
                    continue
                confidence = _context_confidence(text, start, end, self.config)
                matches.append(
                    PIIMatch(
                        pii_type=self.pii_type,
                        value=text[start:end],
                        start=start,
                        end=end,
                        confidence=confidence,
                    )
                )
        return matches

    @staticmethod
    def _trim_start(text: str, start: int) -> int:
        while start < len(text) and text[start] in " \t":
            start += 1
        return start

    @staticmethod
    def _value_end(text: str, start: int) -> int:
        end = start
        while end < len(text):
            character = text[end]
            if character in ",;!?\n":
                break
            if character == ".":
                # Keep abbreviations like "г. Москва" and "ул. Ленина" inside.
                if _is_abbreviation_continuation(text, end):
                    end += 1
                    continue
                break
            # Stop before a date (ISO YYYY-MM-DD or numeric DD.MM.YYYY) so a
            # following issue date is not swallowed by the authority span.
            if _is_date_start(text, end):
                break
            end += 1
        # Trim trailing whitespace so the span ends exactly at the value.
        while end > start and text[end - 1] in " \t":
            end -= 1
        return end


class PassportUnitCodeDetector:
    """Detect the passport unit code in the XXX-XXX format."""

    pii_type = "PASSPORT_UNIT_CODE"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "код подразделения": 0.40,
                "код подр": 0.40,
                "код подр.": 0.40,
                "подразделение": 0.30,
                "подр": 0.30,
                "подр.": 0.30,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in PASSPORT_UNIT_CODE_PATTERN.finditer(text):
            confidence = _context_confidence(
                text, match.start(), match.end(), self.config
            )
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        return matches


class PassportIssueDateDetector:
    """Detect the passport issue date anchored by context markers."""

    pii_type = "PASSPORT_ISSUE_DATE"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            context_window_chars=120,
            distance_decay_power=0.5,
            positive_context_weights={
                "дата выдачи": 0.50,
                "выдан": 0.45,
                "выдано": 0.45,
                "выдана": 0.45,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in DATE_NUMERIC_PATTERN.finditer(text):
            parsed = DateOfBirthDetector._parse_numeric(match)
            if parsed is None:
                continue
            confidence = _context_confidence(
                text, match.start(), match.end(), self.config
            )
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        matches.extend(self._textual_date_matches(text))
        return matches

    def _textual_date_matches(self, text: str) -> list[PIIMatch]:
        """Detect textual issue dates: '11 декабря 2005 г.'."""
        matches: list[PIIMatch] = []
        for match in DATE_TEXT_PATTERN.finditer(text):
            day = RUSSIAN_DAY_WORDS.get(match.group("day").casefold())
            month = RUSSIAN_MONTHS.get(match.group("month").casefold())
            year_text = match.group("year")
            year = int(year_text) if year_text else None
            if day is None or month is None:
                continue
            if year is not None and not _is_valid_calendar_date(day, month, year):
                continue
            confidence = _context_confidence(
                text, match.start(), match.end(), self.config
            )
            if confidence < 0.80:
                continue
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        for match in NUMERIC_DAY_MONTH_PATTERN.finditer(text):
            day = int(match.group("day"))
            month = RUSSIAN_MONTHS.get(match.group("month").casefold())
            year_text = match.group("year")
            year = int(year_text) if year_text else None
            if month is None:
                continue
            if year is not None and not _is_valid_calendar_date(day, month, year):
                continue
            confidence = _context_confidence(
                text, match.start(), match.end(), self.config
            )
            if confidence < 0.80:
                continue
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        return matches


# ---------------------------------------------------------------------------
# Driving license.
# ---------------------------------------------------------------------------

DRIVING_LICENSE_PATTERN = re.compile(
    r"(?<![0-9])(?P<series>[0-9]{2}[ -]?[0-9]{2})"
    r"[ \t]+(?P<number>[0-9]{6})(?![0-9])"
)


class DrivingLicenseDetector:
    """Detect a Russian driving license series and number."""

    pii_type = "DRIVING_LICENSE"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "водительское удостоверение": 0.40,
                "водительского удостоверения": 0.40,
                "водительские права": 0.40,
                "водительских прав": 0.40,
                "права": 0.30,
                "прав": 0.30,
                "удостоверение": 0.30,
                "удостоверения": 0.30,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in DRIVING_LICENSE_PATTERN.finditer(text):
            confidence = _context_confidence(
                text, match.start(), match.end(), self.config
            )
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        return matches


# ---------------------------------------------------------------------------
# Address.
# ---------------------------------------------------------------------------


class AddressDetector:
    """Detect full address spans using the structural two-stage detector.

    Delegates to ``app.address_detector`` which finds addresses by anchors
    (street/house/zip/geo markers) and expands boundaries, works without an
    explicit marker, handles dirty MDM records, and trims to sentence
    boundaries to avoid capturing trailing prose.
    """

    pii_type = "ADDRESS"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for span in _detect_addresses(text):
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=span["text"],
                    start=span["start"],
                    end=span["end"],
                    confidence=0.95,
                )
            )
        return matches

    def parse(self, text: str) -> list[dict]:
        """Split detected addresses into granules (zip, region, city, ...)."""
        from app.address_detector import parse as _parse_addresses

        return _parse_addresses(text)


# ---------------------------------------------------------------------------
# CVV and PIN.
# ---------------------------------------------------------------------------

CVV_PATTERN = re.compile(r"(?<![0-9])[0-9]{3}(?![0-9])")
PIN_PATTERN = re.compile(r"(?<![0-9])[0-9]{4}(?![0-9])")


class CVVDetector:
    """Detect a card CVV code anchored by context markers."""

    pii_type = "CVV"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "cvv": 0.45,
                "cvc": 0.45,
                "код безопасности": 0.40,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in CVV_PATTERN.finditer(text):
            confidence = _context_confidence(
                text, match.start(), match.end(), self.config
            )
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        return matches


class PinCodeDetector:
    """Detect a card PIN anchored by context markers."""

    pii_type = "PIN"

    def __init__(
        self,
        config: ContextConfig | None = None,
        require_card: bool = True,
    ) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "пин": 0.45,
                "пин-код": 0.45,
                "пин код": 0.45,
                "pin": 0.45,
            },
        )
        self._require_card = require_card

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        has_card = any(
            _passes_luhn(_digits(match.group(0)))
            for match in CARD_PATTERN.finditer(text)
        )
        for match in PIN_PATTERN.finditer(text):
            confidence = _context_confidence(
                text, match.start(), match.end(), self.config
            )
            # Combination rule: PIN is masked only near a card number when
            # require_card is enabled (configurable per consumer).
            if self._require_card and not has_card:
                confidence = min(confidence, 0.50)
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        return matches


# ---------------------------------------------------------------------------
# Card holder name.
# ---------------------------------------------------------------------------

CARD_HOLDER_PATTERN = re.compile(
    r"(?<![A-Za-z])(?P<name>[A-Z]{2,}(?:[ -][A-Z]{2,})+)(?![A-Za-z])"
)


class CardHolderDetector:
    """Detect a card holder name in the NAME SURNAME Latin format."""

    pii_type = "CARD_HOLDER"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "cardholder": 0.45,
                "card holder": 0.45,
                "держатель карты": 0.40,
                "имя держателя": 0.40,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in CARD_HOLDER_PATTERN.finditer(text):
            confidence = _context_confidence(
                text, match.start(), match.end(), self.config
            )
            matches.append(
                PIIMatch(
                    pii_type=self.pii_type,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                )
            )
        return matches


# ---------------------------------------------------------------------------
# Russian full-name detection (rule-based pre-check for NER).
# ---------------------------------------------------------------------------

# Cheap pre-filter: a run of 1-4 capitalized Russian words. This regex is
# intentionally broad; the dataset membership check does the real filtering.
NAME_CANDIDATE_PATTERN = re.compile(
    r"(?<![а-яёa-z0-9])"
    r"(?:[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)"
    r"(?:\s+[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?){0,3}"
    r"(?![а-яёa-z0-9])"
)

# Transliterated/English full names: 2-3 capitalized Latin words (Title Case
# or ALL CAPS). Requires context to avoid false positives on arbitrary words.
LATIN_NAME_PATTERN = re.compile(
    r"(?<![A-Za-z])"
    r"(?:[A-Z][a-z]+(?:-[A-Z][a-z]+)?|[A-Z]{2,}(?:-[A-Z]{2,})?)"
    r"(?:\s+(?:[A-Z][a-z]+(?:-[A-Z][a-z]+)?|[A-Z]{2,}(?:-[A-Z]{2,})?)){1,2}"
    r"(?![A-Za-z])"
)


class NameDetector:
    """Detect Russian full names (ФИО) from a local dataset without NER.

    The detector is a cheap rule-based pre-check: it finds runs of capitalized
    Russian words and validates them against the offline dataset of first
    names, surnames and patronymics. When it produces matches, the caller can
    skip the expensive NER inference for the same text.
    """

    pii_type = "PERSON"

    def __init__(self, confidence: float = 0.95) -> None:
        self._confidence = confidence
        self._latin_context = ContextConfig(
            positive_context_weights={
                "name": 0.45,
                "full name": 0.50,
                "зовут": 0.40,
                "фио": 0.45,
                "cardholder": 0.45,
                "card holder": 0.45,
                "держатель": 0.40,
                "владелец": 0.40,
            },
        )
        # Markers that strongly indicate a full name follows, allowing
        # recognition of names not present in the offline dataset.
        self._name_context = ContextConfig(
            positive_context_weights={
                "фио": 0.99,
                "зовут": 0.99,
                "клиент": 0.90,
                "клиента": 0.90,
                "профиль": 0.90,
                "профиля": 0.90,
                "предприниматель": 0.90,
                "предпринимателя": 0.90,
                "обращение": 0.85,
                "обращения": 0.85,
                "проверка благонадёжности": 0.90,
                "проверка благонадежности": 0.90,
                "актуализировать профиль": 0.90,
                "история обращения": 0.85,
                "заявка на проверку": 0.90,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for candidate in NAME_CANDIDATE_PATTERN.finditer(text):
            tokens = candidate.group(0).split()
            # Prefer the longest full-name span inside the candidate run.
            for length in (3, 2):
                for start in range(len(tokens) - length + 1):
                    span = tokens[start : start + length]
                    if not self._is_full_name(span):
                        continue
                    value = " ".join(span)
                    if is_known_person(value):
                        continue
                    offset = candidate.start() + candidate.group(0).index(value)
                    matches.append(
                        PIIMatch(
                            pii_type=self.pii_type,
                            value=value,
                            start=offset,
                            end=offset + len(value),
                            confidence=self._confidence,
                        )
                    )
                    break
                else:
                    continue
                break
        matches.extend(self._latin_name_matches(text))
        matches.extend(self._context_name_matches(text))
        return matches

    def _latin_name_matches(self, text: str) -> list[PIIMatch]:
            """Detect transliterated/English full names.

            Three-word runs (Фамилия Имя Отчество) are structurally a full name and
            are masked without context. Two-word runs require context to avoid false
            positives on arbitrary capitalized words.
            """
            matches: list[PIIMatch] = []
            for candidate in LATIN_NAME_PATTERN.finditer(text):
                token_count = len(candidate.group(0).split())
                if token_count >= 3:
                    matches.append(
                        PIIMatch(
                            pii_type=self.pii_type,
                            value=candidate.group(0),
                            start=candidate.start(),
                            end=candidate.end(),
                            confidence=self._confidence,
                        )
                    )
                    continue
                confidence = _context_confidence(
                    text, candidate.start(), candidate.end(), self._latin_context
                )
                if confidence < 0.80:
                    continue
                matches.append(
                    PIIMatch(
                        pii_type=self.pii_type,
                        value=candidate.group(0),
                        start=candidate.start(),
                        end=candidate.end(),
                        confidence=confidence,
                    )
                )
            return matches

    def _context_name_matches(self, text: str) -> list[PIIMatch]:
        """Detect full names after strong context markers (ФИО:, зовут, клиент).

        Names not present in the offline dataset are still recognized when a
        strong marker indicates a full name follows. Supports lowercase names
        after "клиент" (e.g. "клиент тулаев ибрагим идрисович").
        """
        matches: list[PIIMatch] = []
        normalized = text.casefold()
        for marker in self._name_context.positive_context_weights:
            for found in re.finditer(re.escape(marker), normalized):
                start = found.end()
                while start < len(text) and text[start] in " \t:;—–-":
                    start += 1
                # Capture 2-3 capitalized words (or lowercase after "клиент").
                if marker in ("клиент", "клиента"):
                    name_re = re.compile(
                        r"[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)?"
                        r"(?:\s+[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)?){1,2}"
                    )
                else:
                    name_re = re.compile(
                        r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
                        r"(?:\s+[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?){1,2}"
                    )
                m = name_re.match(text, start)
                if not m:
                    continue
                value = m.group(0)
                if is_known_person(value):
                    continue
                matches.append(
                    PIIMatch(
                        pii_type=self.pii_type,
                        value=value,
                        start=m.start(),
                        end=m.end(),
                        confidence=self._name_context.positive_context_weights[marker],
                    )
                )
        return matches

    @staticmethod
    def _is_full_name(tokens: Sequence[str]) -> bool:
        """Return True when a token run looks like a Russian full name."""
        if not tokens:
            return False
        lowered = [token.casefold() for token in tokens]

        # Single token: only a surname or a first name is too weak alone.
        if len(lowered) == 1:
            return False

        # Two tokens: "Имя Фамилия" or "Фамилия Имя".
        if len(lowered) == 2:
            first, second = lowered
            return (first in FIRST_NAMES and _is_surname(second)) or (
                _is_surname(first) and second in FIRST_NAMES
            )

        # Three tokens: "Фамилия Имя Отчество" or "Имя Отчество Фамилия".
        if len(lowered) == 3:
            a, b, c = lowered
            if _is_surname(a) and b in FIRST_NAMES and c in PATRONYMICS:
                return True
            return a in FIRST_NAMES and b in PATRONYMICS and _is_surname(c)

        # Four tokens: "Фамилия Имя Отчество" plus an extra token is unlikely.
        return False


def _is_surname(token: str) -> bool:
    """Return True for a masculine or feminine Russian surname form."""
    if token in SURNAMES:
        return True
    # Feminine surnames usually end in -а/-я (Смирнова, Иванова, Кузнецова).
    return token.endswith(("а", "я")) and token[:-1] in SURNAMES


# ---------------------------------------------------------------------------
# Known-person suppression for NER PERSON matches.
# ---------------------------------------------------------------------------

KNOWN_PERSONS = frozenset(
    {
        # Full names with patronymic (for suppression of "Имя Отчество Фамилия").
        "александр сергеевич пушкин",
        "лев николаевич толстой",
        "фёдор михайлович достоевский",
        "антон павлович чехов",
        "михаил юрьевич лермонтов",
        "николай васильевич гоголь",
        "сергей александрович есенин",
        "владимир владимирович маяковский",
        "иван алексеевич бунин",
        "александр александрович блок",
        "марина ивановна цветаева",
        "анна андреевна ахматова",
        "борис леонидович пастернак",
        "иосиф александрович бродский",
        "михаил афанасьевич булгаков",
        "иван сергеевич тургенев",
        "александр сергеевич грибоедов",
        "николай алексеевич некрасов",
        "афанасий афанасьевич фет",
        "василий андреевич жуковский",
        "константин дмитриевич бальмонт",
        "валерий яковлевич брюсов",
        "андрей белый",
        "александр иванович куприн",
        "максим горький",
        "аркадий петрович гайдар",
        "самуил яковлевич маршак",
        "корней иванович чуковский",
        "алексей николаевич толстой",
        "михаил александрович шолохов",
        "александр исаевич солженицын",
        "василий макарович шукшин",
        "виктор петрович астафьев",
        "валентин григорьевич распутин",
        "юрий карлович олеша",
        "илья ильф",
        "евгений петров",
        "аркадий натанович стругацкий",
        "борис натанович стругацкий",
        # Short names / surnames.
        "александр пушкин",
        "пушкин",
        "лев толстой",
        "толстой",
        "фёдор достоевский",
        "достоевский",
        "антон чехов",
        "чехов",
        "михаил лермонтов",
        "лермонтов",
        "николай гоголь",
        "гоголь",
        "сергей есенин",
        "есенин",
        "владимир маяковский",
        "маяковский",
        "иван бунин",
        "бунин",
        "александр блок",
        "блок",
        "марина цветаева",
        "цветаева",
        "анна ахматова",
        "ахматова",
        "борис пастернак",
        "пастернак",
        "иосиф бродский",
        "бродский",
        "михаил булгаков",
        "булгаков",
        "иван тургенев",
        "тургенев",
        "александр грибоедов",
        "грибоедов",
        "николай некрасов",
        "некрасов",
        "афанасий фет",
        "фет",
        "василий жуковский",
        "жуковский",
        "константин бальмонт",
        "бальмонт",
        "валерий брюсов",
        "брюсов",
        "белый",
        "александр куприн",
        "куприн",
        "горький",
        "аркадий гайдар",
        "гайдар",
        "самуил маршак",
        "маршак",
        "корней чуковский",
        "чуковский",
        "алексей толстой",
        "михаил шолохов",
        "шолохов",
        "александр солженицын",
        "солженицын",
        "василий шукшин",
        "шукшин",
        "виктор астафьев",
        "астафьев",
        "валентин распутин",
        "распутин",
        "юрий олеша",
        "олеша",
        "ильф",
        "петров",
        "аркадий стругацкий",
        "стругацкий",
        "борис стругацкий",
    }
)


def is_known_person(value: str) -> bool:
    """Return True when a PERSON span refers to a well-known public figure."""
    normalized = " ".join(value.casefold().split())
    if normalized in KNOWN_PERSONS:
        return True
    return any(
        normalized.endswith(surname) or normalized.startswith(surname)
        for surname in KNOWN_PERSONS
        if " " in surname
    )


# Conventional CapWords aliases remain convenient for callers that prefer them.
SnilsDetector = SNILSDetector
InnDetector = INNDetector


def default_rule_detectors(
    require_card_for_pin: bool = True,
) -> tuple[PIIDetector, ...]:
    """Build the dependency-free detector set used when NER is disabled."""

    return (
        EmailDetector(),
        PhoneDetector(),
        PassportDetector(),
        SNILSDetector(),
        INNDetector(),
        KPPDetector(),
        OGRNDetector(),
        CardDetector(),
        DateOfBirthDetector(),
        BirthPlaceDetector(),
        CitizenshipDetector(),
        PassportAuthorityDetector(),
        PassportUnitCodeDetector(),
        PassportIssueDateDetector(),
        DrivingLicenseDetector(),
        AddressDetector(),
        CVVDetector(),
        PinCodeDetector(require_card=require_card_for_pin),
        CardHolderDetector(),
        NameDetector(),
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
        enabled_pii_types: frozenset[str] | None = None,
        masking_mode: str = "typed_placeholder",
        ml_detectors: Sequence[PIIDetector] | None = None,
        degradation: str = "fail_closed",
        require_card_for_pin: bool = True,
    ) -> None:
        self.mapping: dict[str, str] = {}
        self.decisions: list[PIIDecision] = []
        self._counters: dict[str, int] = {}
        self._pii_types: set[str] = set()
        self._matches: list[PIIMatch] = []
        if detectors is not None:
            self._detectors = tuple(
                self._with_pin_rule(detectors, require_card_for_pin)
            )
        else:
            self._detectors = default_rule_detectors(
                require_card_for_pin=require_card_for_pin
            )
        self._ml_detectors = tuple(ml_detectors or ())
        self._min_confidence = min_confidence
        self._enabled_pii_types = enabled_pii_types
        self._masking_mode = masking_mode
        self._degradation = degradation

    @staticmethod
    def _with_pin_rule(
        detectors: Sequence[PIIDetector], require_card: bool
    ) -> list[PIIDetector]:
        """Replace the PIN detector so its combination rule matches the policy."""
        rebuilt: list[PIIDetector] = []
        for detector in detectors:
            if isinstance(detector, PinCodeDetector):
                rebuilt.append(PinCodeDetector(require_card=require_card))
            else:
                rebuilt.append(detector)
        return rebuilt

    def mask(self, text: str) -> str:
        candidates: list[PIIMatch] = []
        for detector in self._detectors:
            candidates.extend(detector.detect(text))
        for detector in self._ml_detectors:
            try:
                candidates.extend(detector.detect(text))
            except Exception:
                if self._degradation == "rule_only":
                    # Degrade gracefully: skip the ML detector and continue
                    # with rule-based detectors only.
                    continue
                raise
        candidates.sort(key=lambda match: (match.start, match.end, match.pii_type))
        eligible = [
            match
            for match in candidates
            if match.confidence >= self._min_confidence
            and self._is_enabled(match.pii_type)
        ]
        matches = resolve_overlapping_matches(eligible)
        self._matches = matches
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
            placeholder = self._placeholder(
                match.pii_type, self._counters[match.pii_type], match.value
            )
            self.mapping[placeholder] = match.value
            self._pii_types.add(match.pii_type)
            masked_parts.append(placeholder)
            previous_end = match.end

        masked_parts.append(text[previous_end:])
        return "".join(masked_parts)

    def _is_enabled(self, pii_type: str) -> bool:
        if self._enabled_pii_types is None:
            return True
        return pii_type in self._enabled_pii_types

    def _placeholder(self, pii_type: str, index: int, value: str) -> str:
        if self._masking_mode == "typed_placeholder":
            return f"__PII_{pii_type}_{index}__"
        if self._masking_mode == "synthetic":
            return self._synthetic_value(pii_type, index)
        if self._masking_mode == "format_preserving":
            return self._format_preserving(value, index)
        return f"__PII_{pii_type}_{index}__"

    @staticmethod
    def _synthetic_value(pii_type: str, index: int) -> str:
        """Fixed synthetic replacement per PII type (unique per index)."""
        base = {
            "PERSON": "Иванов Иван Иванович",
            "DATE_OF_BIRTH": "01.01.1990",
            "BIRTH_PLACE": "г. Москва",
            "PASSPORT": "45 10 123456",
            "CITIZENSHIP": "Российская Федерация",
            "PASSPORT_AUTHORITY": "УФМС России",
            "PASSPORT_UNIT_CODE": "770-001",
            "PASSPORT_ISSUE_DATE": "01.01.2010",
            "DRIVING_LICENSE": "77 01 123456",
            "ADDRESS": "г. Москва, ул. Тестовая, д. 1",
            "EMAIL": "user@example.com",
            "PHONE": "+7 900 000 00 00",
            "INN": "770708389301",
            "CARD": "4000 0000 0000 0000",
            "CVV": "000",
            "PIN": "0000",
            "CARD_HOLDER": "IVANOV IVAN",
        }.get(pii_type, "PII")
        return f"{base}_{index}"

    @staticmethod
    def _format_preserving(value: str, index: int) -> str:
        """Replace each non-space char with '*' keeping length and separators."""
        out: list[str] = []
        for ch in value:
            if ch.isspace():
                out.append(" ")
            else:
                out.append("*")
        return "".join(out)

    def result(self, text: str) -> MaskingResult:
        return MaskingResult(text=self.mask(text), mapping=dict(self.mapping))

    @property
    def pii_types(self) -> list[str]:
        return sorted(self._pii_types)

    @property
    def matches(self) -> list[PIIMatch]:
        """Final resolved matches after overlap resolution (used for masking)."""
        return list(self._matches)


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
