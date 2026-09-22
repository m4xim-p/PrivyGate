"""Request-scoped PII masking and boundary-safe streaming demasking."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

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
        self._driving_context = ContextConfig(
            positive_context_weights={
                "водительское удостоверение": 0.99,
                "водительские права": 0.99,
                "права": 0.99,
                "удостоверение": 0.99,
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
    if any(boundary in between for boundary in config.hard_context_boundaries):
        return 0.0
    if any(boundary in between for boundary in config.soft_context_boundaries):
        return config.soft_boundary_multiplier
    return 1.0


def _is_abbreviation_continuation(text: str, period_index: int) -> bool:
    """Return True when a period starts an abbreviation like 'г.' or 'ул.'."""
    next_index = period_index + 1
    while next_index < len(text) and text[next_index] in " \t":
        next_index += 1
    if next_index >= len(text):
        return False
    return text[next_index].isalpha()


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
            day = RUSSIAN_DAY_WORDS.get(match.group("day").casefold())
            month = RUSSIAN_MONTHS.get(match.group("month").casefold())
            year_text = match.group("year")
            year = int(year_text) if year_text else None
            if day is None or month is None:
                continue
            if year is not None and not _is_valid_calendar_date(day, month, year):
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

        # dd.mm.yyyy / mm.dd.yyyy
        if not (1900 <= third <= 2100):
            return None
        if _is_valid_calendar_date(first, second, third):
            return first, second, third
        if _is_valid_calendar_date(second, first, third):
            return second, first, third
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
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        normalized = text.casefold()
        for phrase in self.config.positive_context_weights:
            for marker in re.finditer(re.escape(phrase.casefold()), normalized):
                start = self._trim_start(text, marker.end())
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
                if end + 1 < len(text) and text[end + 1].isalpha():
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
            end += 1
        return end


class PassportUnitCodeDetector:
    """Detect the passport unit code in the XXX-XXX format."""

    pii_type = "PASSPORT_UNIT_CODE"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "код подразделения": 0.40,
                "подразделение": 0.30,
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
            positive_context_weights={
                "дата выдачи": 0.40,
                "выдан": 0.30,
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
                "водительские права": 0.40,
                "права": 0.30,
                "удостоверение": 0.30,
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

POSTAL_CODE_PATTERN = re.compile(r"(?<![0-9])[0-9]{6}(?![0-9])")


class AddressDetector:
    """Detect address components: postal code, city, street, house, apartment."""

    pii_type = "ADDRESS"

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "адрес": 0.40,
                "проживает": 0.35,
                "зарегистрирован": 0.35,
                "зарегистрирована": 0.35,
                "индекс": 0.40,
                "город": 0.35,
                "улица": 0.35,
                "ул.": 0.35,
                "дом": 0.30,
                "квартира": 0.30,
                "кв.": 0.30,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in POSTAL_CODE_PATTERN.finditer(text):
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

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config if config is not None else ContextConfig(
            positive_context_weights={
                "пин": 0.45,
                "пин-код": 0.45,
                "пин код": 0.45,
                "pin": 0.45,
            },
        )

    def detect(self, text: str) -> list[PIIMatch]:
        matches: list[PIIMatch] = []
        for match in PIN_PATTERN.finditer(text):
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
            if a in FIRST_NAMES and b in PATRONYMICS and _is_surname(c):
                return True
            return False

        # Four tokens: "Фамилия Имя Отчество" plus an extra token is unlikely.
        return False


def _is_surname(token: str) -> bool:
    """Return True for a masculine or feminine Russian surname form."""
    if token in SURNAMES:
        return True
    # Feminine surnames usually end in -а/-я (Смирнова, Иванова, Кузнецова).
    if token.endswith(("а", "я")) and token[:-1] in SURNAMES:
        return True
    return False


# ---------------------------------------------------------------------------
# Known-person suppression for NER PERSON matches.
# ---------------------------------------------------------------------------

KNOWN_PERSONS = frozenset(
    {
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
        "андрей белый",
        "белый",
        "александр куприн",
        "куприн",
        "максим горький",
        "горький",
        "аркадий гайдар",
        "гайдар",
        "самуил маршак",
        "маршак",
        "корней чуковский",
        "чуковский",
        "алексей толстой",
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
        "илья ильф",
        "ильф",
        "евгений петров",
        "петров",
        "аркадий стругацкий",
        "стругацкий",
        "борис стругацкий",
        "стругацкий",
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


def default_rule_detectors() -> tuple[PIIDetector, ...]:
    """Build the dependency-free detector set used when NER is disabled."""

    return (
        EmailDetector(),
        PhoneDetector(),
        PassportDetector(),
        SNILSDetector(),
        INNDetector(),
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
        PinCodeDetector(),
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
