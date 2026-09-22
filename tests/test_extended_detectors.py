from app.pii import (
    AddressDetector,
    BirthPlaceDetector,
    CardHolderDetector,
    CitizenshipDetector,
    CVVDetector,
    DateOfBirthDetector,
    DrivingLicenseDetector,
    PassportAuthorityDetector,
    PassportIssueDateDetector,
    PassportUnitCodeDetector,
    PIIMasker,
    PinCodeDetector,
    is_known_person,
)


def test_date_of_birth_numeric_dd_mm_yyyy() -> None:
    text = "Дата рождения 15.03.1990"
    match = DateOfBirthDetector().detect(text)[0]

    assert match.pii_type == "DATE_OF_BIRTH"
    assert match.value == "15.03.1990"
    assert PIIMasker().mask(text) == "Дата рождения __PII_DATE_OF_BIRTH_1__"


def test_date_of_birth_numeric_yyyy_mm_dd() -> None:
    text = "Дата рождения 1990.03.15"
    match = DateOfBirthDetector().detect(text)[0]

    assert match.value == "1990.03.15"


def test_date_of_birth_numeric_mm_dd_yyyy() -> None:
    text = "Дата рождения 03/15/1990"
    match = DateOfBirthDetector().detect(text)[0]

    assert match.value == "03/15/1990"


def test_date_of_birth_invalid_calendar_is_not_detected() -> None:
    text = "Дата рождения 31.02.1990"

    assert DateOfBirthDetector().detect(text) == []


def test_date_of_birth_textual() -> None:
    text = "Дата рождения пятнадцатого марта 1990 года"
    match = DateOfBirthDetector().detect(text)[0]

    assert match.value == "пятнадцатого марта 1990 года"


def test_date_of_birth_textual_without_year() -> None:
    text = "Дата рождения пятнадцатого марта"
    match = DateOfBirthDetector().detect(text)[0]

    assert match.value == "пятнадцатого марта"


def test_date_of_birth_case_insensitive() -> None:
    text = "ДАТА РОЖДЕНИЯ 15.03.1990"
    match = DateOfBirthDetector().detect(text)[0]

    assert match.value == "15.03.1990"


def test_date_of_birth_negative_context_is_not_masked() -> None:
    text = "Срок действия до 15.03.1990"
    matches = DateOfBirthDetector().detect(text)

    assert len(matches) == 1
    assert matches[0].confidence < 0.80
    assert PIIMasker().mask(text) == text


def test_birth_place() -> None:
    text = "Место рождения город Москва"
    match = BirthPlaceDetector().detect(text)[0]

    assert match.pii_type == "BIRTH_PLACE"
    assert match.value == "Москва"
    assert PIIMasker().mask(text) == "Место рождения город __PII_BIRTH_PLACE_1__"


def test_citizenship() -> None:
    text = "Гражданство Российская Федерация"
    match = CitizenshipDetector().detect(text)[0]

    assert match.pii_type == "CITIZENSHIP"
    assert match.value == "Российская Федерация"
    assert PIIMasker().mask(text) == "Гражданство __PII_CITIZENSHIP_1__"


def test_passport_authority() -> None:
    text = "Кем выдан ГУ МВД России по г. Москве"
    match = PassportAuthorityDetector().detect(text)[0]

    assert match.pii_type == "PASSPORT_AUTHORITY"
    assert match.value == "ГУ МВД России по г. Москве"


def test_passport_unit_code() -> None:
    text = "Код подразделения 770-123"
    match = PassportUnitCodeDetector().detect(text)[0]

    assert match.pii_type == "PASSPORT_UNIT_CODE"
    assert match.value == "770-123"
    assert PIIMasker().mask(text) == "Код подразделения __PII_PASSPORT_UNIT_CODE_1__"


def test_passport_issue_date() -> None:
    text = "Дата выдачи 10.05.2015"
    match = PassportIssueDateDetector().detect(text)[0]

    assert match.pii_type == "PASSPORT_ISSUE_DATE"
    assert match.value == "10.05.2015"
    assert PIIMasker().mask(text) == "Дата выдачи __PII_PASSPORT_ISSUE_DATE_1__"


def test_driving_license() -> None:
    text = "Водительское удостоверение 77 15 123456"
    match = DrivingLicenseDetector().detect(text)[0]

    assert match.pii_type == "DRIVING_LICENSE"
    assert match.value == "77 15 123456"
    assert PIIMasker().mask(text) == (
        "Водительское удостоверение __PII_DRIVING_LICENSE_1__"
    )


def test_address_postal_code() -> None:
    text = "Адрес: 101000, город Москва"
    matches = AddressDetector().detect(text)

    assert any(m.pii_type == "ADDRESS" for m in matches)
    assert PIIMasker().mask(text) == "Адрес: __PII_ADDRESS_1__"


def test_cvv() -> None:
    text = "CVV 123"
    match = CVVDetector().detect(text)[0]

    assert match.pii_type == "CVV"
    assert match.value == "123"
    assert PIIMasker().mask(text) == "CVV __PII_CVV_1__"


def test_cvv_without_context_is_not_masked() -> None:
    text = "Число 123"
    matches = CVVDetector().detect(text)

    assert len(matches) == 1
    assert matches[0].confidence < 0.80
    assert PIIMasker().mask(text) == text


def test_pin() -> None:
    text = "Пин-код 1234"
    match = PinCodeDetector().detect(text)[0]

    assert match.pii_type == "PIN"
    assert match.value == "1234"
    assert PIIMasker().mask(text) == "Пин-код __PII_PIN_1__"


def test_pin_without_context_is_not_masked() -> None:
    text = "Число 1234"
    matches = PinCodeDetector().detect(text)

    assert len(matches) == 1
    assert matches[0].confidence < 0.80
    assert PIIMasker().mask(text) == text


def test_card_holder() -> None:
    text = "Cardholder IVAN PETROV"
    match = CardHolderDetector().detect(text)[0]

    assert match.pii_type == "CARD_HOLDER"
    assert match.value == "IVAN PETROV"
    assert PIIMasker().mask(text) == "Cardholder __PII_CARD_HOLDER_1__"


def test_card_holder_without_context_is_not_masked() -> None:
    text = "IVAN PETROV"
    matches = CardHolderDetector().detect(text)

    assert len(matches) == 1
    assert matches[0].confidence < 0.80
    assert PIIMasker().mask(text) == text


def test_known_person_suppression() -> None:
    assert is_known_person("Александр Пушкин") is True
    assert is_known_person("Пушкин") is True
    assert is_known_person("Иван Петров") is False


def test_default_detectors_include_extended_set() -> None:
    from app.pii import default_rule_detectors

    detector_types = {type(detector) for detector in default_rule_detectors()}

    assert DateOfBirthDetector in detector_types
    assert BirthPlaceDetector in detector_types
    assert CitizenshipDetector in detector_types
    assert PassportAuthorityDetector in detector_types
    assert PassportUnitCodeDetector in detector_types
    assert PassportIssueDateDetector in detector_types
    assert DrivingLicenseDetector in detector_types
    assert AddressDetector in detector_types
    assert CVVDetector in detector_types
    assert PinCodeDetector in detector_types
    assert CardHolderDetector in detector_types
