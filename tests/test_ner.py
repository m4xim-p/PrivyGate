import logging
import sys
from types import SimpleNamespace

from app.ner import NERDetector, NERTokenPrediction, TransformersNERBackend
from app.pii import PhoneDetector, PIIMasker


class FakeNERBackend:
    def __init__(self, predictions: list[NERTokenPrediction]) -> None:
        self.predictions = predictions
        self.calls = 0

    def predict(self, text: str) -> list[NERTokenPrediction]:
        self.calls += 1
        return self.predictions


def test_transformers_backend_pins_revision(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    class FakeTokenizer:
        is_fast = True

    class FakeModel:
        def to(self, device: str) -> None:
            self.device = device

        def eval(self) -> None:
            self.evaluated = True

    class FakeTokenizerFactory:
        @staticmethod
        def from_pretrained(model_name: str, **kwargs: object) -> FakeTokenizer:
            calls.append(("tokenizer", model_name, kwargs))
            return FakeTokenizer()

    class FakeModelFactory:
        @staticmethod
        def from_pretrained(model_name: str, **kwargs: object) -> FakeModel:
            calls.append(("model", model_name, kwargs))
            return FakeModel()

    fake_torch = SimpleNamespace(device=lambda value: value)
    fake_transformers = SimpleNamespace(
        AutoModelForTokenClassification=FakeModelFactory,
        AutoTokenizer=FakeTokenizerFactory,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    TransformersNERBackend.from_pretrained("example/model", revision="commit-sha")

    assert calls == [
        ("tokenizer", "example/model", {"revision": "commit-sha", "use_fast": True}),
        ("model", "example/model", {"revision": "commit-sha"}),
    ]


def person_tokens(
    text: str,
    first_name: str,
    last_name: str,
) -> list[NERTokenPrediction]:
    first_start = text.index(first_name)
    last_start = text.index(last_name)
    return [
        NERTokenPrediction(
            "B-PER",
            first_start,
            first_start + len(first_name),
            0.96,
        ),
        NERTokenPrediction(
            "I-PER",
            last_start,
            last_start + len(last_name),
            0.94,
        ),
    ]


def test_detects_person_in_introduction() -> None:
    text = "Меня зовут Иван Петров"
    backend = FakeNERBackend(person_tokens(text, "Иван", "Петров"))
    detector = NERDetector(backend)

    matches = detector.detect(text)

    assert len(matches) == 1
    assert matches[0].pii_type == "PERSON"
    assert matches[0].value == "Иван Петров"
    assert text[matches[0].start : matches[0].end] == "Иван Петров"
    assert matches[0].confidence == 0.94
    assert backend.calls == 1


def test_detects_inflected_person_name() -> None:
    text = "Позвони Ивану Петрову завтра"
    detector = NERDetector(
        FakeNERBackend(person_tokens(text, "Ивану", "Петрову"))
    )

    assert PIIMasker(detectors=[detector]).mask(text) == (
        "Позвони __PII_PERSON_1__ завтра"
    )


def test_does_not_map_organization_to_person() -> None:
    text = "Компания Иван Петров работает с клиентами"
    first_start = text.index("Иван")
    last_start = text.index("Петров")
    backend = FakeNERBackend(
        [
            NERTokenPrediction("B-ORG", first_start, first_start + 4, 0.97),
            NERTokenPrediction("I-ORG", last_start, last_start + 6, 0.96),
        ]
    )

    assert NERDetector(backend).detect(text) == []


def test_person_and_phone_are_merged_by_existing_masker() -> None:
    text = "Иван Петров, телефон +7 999 123-45-67"
    ner_detector = NERDetector(
        FakeNERBackend(person_tokens(text, "Иван", "Петров"))
    )
    masker = PIIMasker(detectors=[ner_detector, PhoneDetector()])

    assert masker.mask(text) == (
        "__PII_PERSON_1__, телефон __PII_PHONE_1__"
    )
    assert masker.pii_types == ["PERSON", "PHONE"]


def test_person_label_alias_and_confidence_threshold() -> None:
    text = "Анна ответила"
    start = text.index("Анна")
    detector = NERDetector(
        FakeNERBackend(
            [NERTokenPrediction("B-PERSON", start, start + len("Анна"), 0.70)]
        )
    )

    assert detector.detect(text) == []


def test_timing_log_contains_no_input_text(caplog) -> None:
    text = "Меня зовут Иван Петров"
    detector = NERDetector(
        FakeNERBackend(person_tokens(text, "Иван", "Петров"))
    )

    with caplog.at_level(logging.INFO, logger="privygate.ner"):
        detector.detect(text)

    assert "ner_inference_finished" in caplog.text
    assert "latency_ms=" in caplog.text
    assert "person_entities=1" in caplog.text
    assert "suspicious_person_span=false" in caplog.text
    assert text not in caplog.text
    assert "Иван" not in caplog.text


def test_suspicious_person_span_is_preserved_with_original_offsets(caplog) -> None:
    text = "Меня зовут Иван Петров Позвони Ивану Петрову завтра"
    person_words = ["Иван", "Петров", "Позвони", "Ивану", "Петрову"]
    predictions = []
    search_from = 0
    for index, word in enumerate(person_words):
        start = text.index(word, search_from)
        predictions.append(
            NERTokenPrediction(
                "B-PER" if index == 0 else "I-PER",
                start,
                start + len(word),
                0.95,
            )
        )
        search_from = start + len(word)

    with caplog.at_level(logging.INFO, logger="privygate.ner"):
        matches = NERDetector(FakeNERBackend(predictions)).detect(text)

    expected_value = "Иван Петров Позвони Ивану Петрову"
    expected_start = text.index("Иван")
    expected_end = expected_start + len(expected_value)
    assert len(matches) == 1
    assert matches[0].value == expected_value
    assert matches[0].start == expected_start
    assert matches[0].end == expected_end
    assert text[matches[0].start : matches[0].end] == expected_value
    assert "suspicious_person_span=true" in caplog.text
    assert expected_value not in caplog.text
    assert "Иван" not in caplog.text
    assert "Петрову" not in caplog.text


def test_suspicious_person_span_splits_at_bio_restart() -> None:
    text = "Меня зовут Иван Петров Позвони Ивану Петрову завтра"
    words_and_labels = [
        ("Иван", "B-PER"),
        ("Петров", "I-PER"),
        ("Позвони", "I-PER"),
        ("Ивану", "B-PER"),
        ("Петрову", "I-PER"),
    ]
    predictions = []
    search_from = 0
    for word, label in words_and_labels:
        start = text.index(word, search_from)
        predictions.append(NERTokenPrediction(label, start, start + len(word), 0.95))
        search_from = start + len(word)

    matches = NERDetector(FakeNERBackend(predictions)).detect(text)

    assert [match.value for match in matches] == [
        "Иван Петров Позвони",
        "Ивану Петрову",
    ]
    assert all(text[match.start : match.end] == match.value for match in matches)
