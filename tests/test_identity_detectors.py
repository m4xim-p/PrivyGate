from app.pii import (
    CardDetector,
    INNDetector,
    PassportDetector,
    PIIMasker,
    SNILSDetector,
)


def test_valid_russian_passport_is_masked() -> None:
    text = "Паспорт 45 10 123456"
    match = PassportDetector().detect(text)[0]

    assert match.pii_type == "PASSPORT"
    assert match.value == "45 10 123456"
    assert text[match.start : match.end] == match.value
    assert PIIMasker().mask(text) == "Паспорт __PII_PASSPORT_1__"


def test_supported_russian_passport_formats() -> None:
    for passport in (
        "45 10 123456",
        "4510 123456",
        "45-10 № 123456",
    ):
        matches = PassportDetector().detect(passport)

        assert len(matches) == 1
        assert matches[0].value == passport


def test_invalid_russian_passport_is_not_detected() -> None:
    text = "Паспорт 00 10 123456"

    assert PassportDetector().detect(text) == []
    assert PIIMasker().mask(text) == text


def test_valid_snils_is_masked() -> None:
    text = "СНИЛС 112-233-445 95"
    match = SNILSDetector().detect(text)[0]

    assert match.pii_type == "SNILS"
    assert match.value == "112-233-445 95"
    assert PIIMasker().mask(text) == "СНИЛС __PII_SNILS_1__"


def test_snils_with_invalid_checksum_is_not_detected() -> None:
    text = "СНИЛС 112-233-445 96"

    assert SNILSDetector().detect(text) == []
    assert PIIMasker().mask(text) == text


def test_valid_ten_digit_inn_is_masked() -> None:
    text = "ИНН организации 7707083893"
    match = INNDetector().detect(text)[0]

    assert match.pii_type == "INN"
    assert match.value == "7707083893"
    assert PIIMasker().mask(text) == "ИНН организации __PII_INN_1__"


def test_valid_twelve_digit_inn_is_masked() -> None:
    text = "ИНН физлица 500100732259"

    assert PIIMasker().mask(text) == "ИНН физлица __PII_INN_1__"


def test_inn_with_invalid_checksum_is_not_detected() -> None:
    for inn in ("7707083894", "500100732258"):
        text = f"ИНН {inn}"

        assert INNDetector().detect(text) == []
        assert PIIMasker().mask(text) == text


def test_valid_card_is_masked() -> None:
    text = "Карта 4111 1111 1111 1111"
    match = CardDetector().detect(text)[0]

    assert match.pii_type == "CARD"
    assert match.value == "4111 1111 1111 1111"
    assert PIIMasker().mask(text) == "Карта __PII_CARD_1__"


def test_card_with_invalid_luhn_checksum_is_not_detected() -> None:
    text = "Карта 4111 1111 1111 1112"

    assert CardDetector().detect(text) == []
    assert PIIMasker().mask(text) == text


def test_foreign_citizen_passport_alpha_numeric_is_masked() -> None:
    text = "Паспорт иностранного гражданина AB1234567"
    match = PassportDetector().detect(text)[0]

    assert match.pii_type == "PASSPORT"
    assert match.value == "AB1234567"
    assert PIIMasker().mask(text) == (
        "Паспорт иностранного гражданина __PII_PASSPORT_1__"
    )


def test_foreign_citizen_passport_requires_context() -> None:
    text = "AB1234567"

    assert PassportDetector().detect(text) == []
    assert PIIMasker().mask(text) == text


def test_foreign_citizen_passport_uzbekistan_is_masked() -> None:
    text = "Паспорт гражданина Узбекистана AB1234567"
    match = PassportDetector().detect(text)[0]

    assert match.pii_type == "PASSPORT"
    assert match.value == "AB1234567"
    assert PIIMasker().mask(text) == (
        "Паспорт гражданина Узбекистана __PII_PASSPORT_1__"
    )
