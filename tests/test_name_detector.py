from app.ner import NERDetector, NERTokenPrediction
from app.pii import (
    NameDetector,
    PIIMasker,
    default_rule_detectors,
)


def test_detects_full_name_first_last() -> None:
    text = "Меня зовут Иван Петров"
    match = NameDetector().detect(text)[0]

    assert match.pii_type == "PERSON"
    assert match.value == "Иван Петров"
    assert PIIMasker().mask(text) == "Меня зовут __PII_PERSON_1__"


def test_detects_full_name_after_leading_word() -> None:
    """A full name preceded by a capitalized word must still be detected."""
    text = "Клиент Иванов Иван Иванович"
    match = NameDetector().detect(text)[0]

    assert match.value == "Иванов Иван Иванович"
    assert PIIMasker().mask(text) == "Клиент __PII_PERSON_1__"


def test_detects_latin_three_word_name_without_context() -> None:
    text = "IVAN PETROV ALEKSEEVICH"
    match = NameDetector().detect(text)[0]

    assert match.value == "IVAN PETROV ALEKSEEVICH"


def test_detects_latin_two_word_name_with_context() -> None:
    text = "Cardholder IVAN PETROV"
    match = NameDetector().detect(text)[0]

    assert match.value == "Cardholder IVAN PETROV"


def test_latin_two_word_name_without_context_is_not_detected() -> None:
    text = "IVAN PETROV"

    assert NameDetector().detect(text) == []


def test_detects_full_name_last_first_patronymic() -> None:
    text = "Петров Иван Иванович"
    match = NameDetector().detect(text)[0]

    assert match.value == "Петров Иван Иванович"


def test_detects_full_name_first_patronymic_last() -> None:
    text = "Иван Иванович Петров"
    match = NameDetector().detect(text)[0]

    assert match.value == "Иван Иванович Петров"


def test_detects_feminine_surname() -> None:
    text = "Мария Ивановна Смирнова"
    match = NameDetector().detect(text)[0]

    assert match.value == "Мария Ивановна Смирнова"


def test_single_name_is_not_detected() -> None:
    text = "Иван"

    assert NameDetector().detect(text) == []


def test_unknown_name_is_not_detected() -> None:
    text = "Квинт Вергилий"

    assert NameDetector().detect(text) == []


def test_known_person_is_not_detected_as_pii() -> None:
    text = "Александр Пушкин"

    assert NameDetector().detect(text) == []


def test_case_insensitive_detection() -> None:
    text = "иван петров"

    assert NameDetector().detect(text) == []


def test_name_detector_is_in_default_set() -> None:
    detector_types = {type(detector) for detector in default_rule_detectors()}

    assert NameDetector in detector_types


class FakeNERBackend:
    def __init__(self) -> None:
        self.calls = 0

    def predict(self, text: str) -> list[NERTokenPrediction]:
        self.calls += 1
        return []


def test_ner_always_runs_inference() -> None:
    """NER always runs even when precheck found a name."""
    backend = FakeNERBackend()
    detector = NERDetector(backend, precheck=NameDetector())

    matches = detector.detect("Меня зовут Иван Петров")

    assert matches == []
    assert backend.calls == 1


def test_ner_precheck_runs_inference_when_no_name_found() -> None:
    backend = FakeNERBackend()
    detector = NERDetector(backend, precheck=NameDetector())

    matches = detector.detect("Компания работает с клиентами")

    assert matches == []
    assert backend.calls == 1
