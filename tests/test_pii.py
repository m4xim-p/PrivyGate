from app.pii import (
    DEFAULT_MASKING_CONFIDENCE,
    EmailDetector,
    PhoneDetector,
    PhoneDetectorConfig,
    PIIMasker,
    StreamingDemasker,
    demask,
)


def test_email_masking() -> None:
    masker = PIIMasker()
    masked = masker.mask("Напиши test@example.com")

    assert masked == "Напиши __PII_EMAIL_1__"
    assert masker.mapping == {"__PII_EMAIL_1__": "test@example.com"}


def test_email_detector_returns_structured_match() -> None:
    text = "Напиши test@example.com"
    matches = EmailDetector().detect(text)

    assert len(matches) == 1
    assert matches[0].pii_type == "EMAIL"
    assert matches[0].value == "test@example.com"
    assert text[matches[0].start : matches[0].end] == matches[0].value
    assert matches[0].confidence >= DEFAULT_MASKING_CONFIDENCE


def test_email_mask_wins_over_phone_candidate_inside_local_part() -> None:
    text = "моя почта 9066778221@mail.ru"
    phone_matches = PhoneDetector().detect(text)
    masker = PIIMasker()

    assert len(phone_matches) == 1
    assert phone_matches[0].value == "9066778221"
    assert masker.mask(text) == "моя почта __PII_EMAIL_1__"
    assert masker.mapping == {"__PII_EMAIL_1__": "9066778221@mail.ru"}
    assert "__PII_PHONE_1__" not in masker.mapping


def test_russian_phone_masking() -> None:
    masker = PIIMasker()
    masked = masker.mask("Позвони на +7 999 123-45-67 или 8 (912) 345 67 89")

    assert masked == "Позвони на __PII_PHONE_1__ или __PII_PHONE_2__"
    assert len(masker.mapping) == 2


def test_phone_with_country_prefix_is_detected() -> None:
    text = "Мой телефон +7 999 123-45-67"
    matches = PhoneDetector().detect(text)

    assert len(matches) == 1
    assert matches[0].pii_type == "PHONE"
    assert matches[0].value == "+7 999 123-45-67"
    assert text[matches[0].start : matches[0].end] == matches[0].value
    assert matches[0].confidence >= DEFAULT_MASKING_CONFIDENCE
    assert PIIMasker().mask(text) == "Мой телефон __PII_PHONE_1__"


def test_ambiguous_phone_is_masked_with_positive_context() -> None:
    text = "Мой телефон 9991234567"
    matches = PhoneDetector().detect(text)
    masker = PIIMasker()

    assert len(matches) == 1
    assert round(matches[0].confidence, 2) == 0.89
    assert matches[0].confidence >= DEFAULT_MASKING_CONFIDENCE
    assert masker.mask(text) == "Мой телефон __PII_PHONE_1__"
    assert masker.decisions[0].action == "masked"
    assert masker.decisions[0].confidence == matches[0].confidence


def test_ambiguous_phone_is_masked_after_phone_number_marker() -> None:
    text = "Номер телефона 9991234567"
    match = PhoneDetector().detect(text)[0]

    assert round(match.confidence, 2) == 0.94
    assert match.confidence >= DEFAULT_MASKING_CONFIDENCE
    assert PIIMasker().mask(text) == "Номер телефона __PII_PHONE_1__"


def test_ambiguous_phone_is_not_masked_with_negative_context() -> None:
    text = "Номер заказа 9991234567"
    matches = PhoneDetector().detect(text)
    masker = PIIMasker()

    assert len(matches) == 1
    assert matches[0].confidence < DEFAULT_MASKING_CONFIDENCE
    assert masker.mask(text) == text
    assert masker.decisions[0].action == "below_threshold"
    assert masker.decisions[0].confidence == matches[0].confidence


def test_mixed_context_is_scored_per_candidate() -> None:
    text = "Заказ 9991234567, мой телефон 8881234567"
    matches = PhoneDetector().detect(text)
    masker = PIIMasker()

    assert len(matches) == 2
    assert matches[0].value == "9991234567"
    assert round(matches[0].confidence, 2) == 0.01
    assert matches[1].value == "8881234567"
    assert round(matches[1].confidence, 2) == 0.89
    assert masker.mask(text) == (
        "Заказ 9991234567, мой телефон __PII_PHONE_1__"
    )
    assert [decision.action for decision in masker.decisions] == [
        "below_threshold",
        "masked",
    ]


def test_phone_context_does_not_cross_hard_punctuation() -> None:
    text = "Телефон указан ранее. Идентификатор 9991234567"
    match = PhoneDetector().detect(text)[0]

    assert match.confidence == 0.55
    assert PIIMasker().mask(text) == text


def test_phone_context_survives_soft_punctuation_with_reduced_weight() -> None:
    text = "Мой телефон: 9991234567"
    match = PhoneDetector().detect(text)[0]

    assert match.confidence >= DEFAULT_MASKING_CONFIDENCE
    assert PIIMasker().mask(text) == "Мой телефон: __PII_PHONE_1__"


def test_unformatted_prefixed_phones_are_supported() -> None:
    for phone in ("89991234567", "+79991234567"):
        matches = PhoneDetector().detect(phone)

        assert len(matches) == 1
        assert matches[0].value == phone
        assert matches[0].confidence >= DEFAULT_MASKING_CONFIDENCE


def test_phone_context_rules_are_configurable() -> None:
    config = PhoneDetectorConfig(
        positive_context_weights={"ватсап": 0.40},
        negative_context_weights={"счёт": 0.60},
    )
    masker = PIIMasker(detectors=[PhoneDetector(config)])

    assert masker.mask("Мой ватсап 9991234567") == (
        "Мой ватсап __PII_PHONE_1__"
    )


def test_demasking() -> None:
    mapping = {
        "__PII_PHONE_1__": "+7 999 123-45-67",
        "__PII_EMAIL_1__": "test@example.com",
    }
    masked = "Телефон __PII_PHONE_1__, email __PII_EMAIL_1__."

    assert demask(masked, mapping) == (
        "Телефон +7 999 123-45-67, email test@example.com."
    )


def test_streaming_demasking_when_placeholder_crosses_chunks() -> None:
    demasker = StreamingDemasker({"__PII_EMAIL_1__": "test@example.com"})

    parts = [demasker.feed("prefix __PII_EM"), demasker.feed("AIL_1__ suffix")]
    parts.append(demasker.flush())

    assert "".join(parts) == "prefix test@example.com suffix"


def test_masked_text_contains_no_raw_pii() -> None:
    raw_email = "private.user@example.org"
    raw_phone = "+7 (999) 111-22-33"
    masker = PIIMasker()

    masked = masker.mask(f"Контакты: {raw_phone}, {raw_email}")

    assert raw_email not in masked
    assert raw_phone not in masked
    assert "__PII_PHONE_1__" in masked
    assert "__PII_EMAIL_1__" in masked
