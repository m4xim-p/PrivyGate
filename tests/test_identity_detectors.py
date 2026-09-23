from app.pii import (
    CardDetector,
    INNDetector,
    PassportDetector,
    PIIMasker,
    SNILSDetector,
    demask,
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


def test_passport_series_number_formats() -> None:
    """Passports with separating words 'серия/номер' mask both numeric parts."""
    cases = {
        "Паспорт: серия 4509 номер 123456": (
            "Паспорт: серия __PII_PASSPORT_1__ номер __PII_PASSPORT_2__"
        ),
        "Паспорт: серия 1234 # 123456": (
            "Паспорт: серия __PII_PASSPORT_1__ # __PII_PASSPORT_2__"
        ),
        "Паспорт: серия: 1234, номер: 123456": (
            "Паспорт: серия: __PII_PASSPORT_1__, номер: __PII_PASSPORT_2__"
        ),
    }
    for text, expected in cases.items():
        assert PIIMasker().mask(text) == expected


def test_passport_series_number_offsets() -> None:
    """Series and number are separate matches with exact offsets."""
    text = "Паспорт: серия 4509 номер 123456"
    matches = PassportDetector().detect(text)

    series = [m for m in matches if m.value == "4509"]
    number = [m for m in matches if m.value == "123456"]
    assert len(series) == 1
    assert len(number) == 1
    assert text[series[0].start : series[0].end] == "4509"
    assert text[number[0].start : number[0].end] == "123456"


def test_passport_series_number_round_trip() -> None:
    """Masking then demasking restores the original passport text."""
    text = "Паспорт: серия 4509 номер 123456"
    masker = PIIMasker()
    masked = masker.mask(text)
    restored = demask(masked, masker.mapping)

    assert restored == text


def test_passport_series_number_requires_context() -> None:
    """'серия ... номер ...' is strong passport context; other words are not."""
    # "серия ... номер ..." alone is a passport pattern.
    assert PIIMasker().mask("серия 4509 номер 123456") == (
        "серия __PII_PASSPORT_1__ номер __PII_PASSPORT_2__"
    )
    # Without "серия", arbitrary numbers are not masked.
    for text in (
        "Число 4509 номер 123456",
        "Код 4509 номер 123456",
    ):
        assert PIIMasker().mask(text) == text


def test_passport_dash_format_is_masked() -> None:
    text = "Паспорт 4509-123456"

    assert PIIMasker().mask(text) == "Паспорт __PII_PASSPORT_1__"


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


def test_military_id_is_masked() -> None:
    text = "Военный билет АБ 1234567"
    match = PassportDetector().detect(text)[0]

    assert match.pii_type == "PASSPORT"
    assert match.value == "АБ 1234567"
    assert PIIMasker().mask(text) == "Военный билет __PII_PASSPORT_1__"


def test_military_id_requires_context() -> None:
    text = "АБ 1234567"

    assert PassportDetector().detect(text) == []
    assert PIIMasker().mask(text) == text
