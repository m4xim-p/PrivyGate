"""Tests for Consumer Policy Registry (ADR-0004)."""

from __future__ import annotations

import json

import pytest

from app.pii import PIIMasker, default_rule_detectors
from app.policy import ConsumerPolicy, PolicyRegistry


def _write_config(tmp_path, consumers: list[dict]) -> str:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"consumers": consumers}), encoding="utf-8")
    return str(path)


def test_default_policy_is_alfasonar() -> None:
    registry = PolicyRegistry()
    policy = registry.resolve(None)
    assert policy.consumer_id == "alfasonar"
    assert policy.enabled is True
    assert policy.allow_demasking is True
    assert policy.masking_mode == "typed_placeholder"


def test_resolve_unknown_consumer_returns_default() -> None:
    registry = PolicyRegistry()
    policy = registry.resolve("unknown-system")
    assert policy.consumer_id == "alfasonar"


def test_resolve_registered_consumer(tmp_path) -> None:
    path = _write_config(
        tmp_path,
        [
            {
                "consumer_id": "crm",
                "enabled": True,
                "enabled_pii_types": ["PERSON", "PHONE"],
                "allow_demasking": False,
                "min_confidence": 0.9,
            }
        ],
    )
    registry = PolicyRegistry(config_path=path)
    policy = registry.resolve("crm")
    assert policy.consumer_id == "crm"
    assert policy.enabled_pii_types == frozenset({"PERSON", "PHONE"})
    assert policy.allow_demasking is False
    assert policy.min_confidence == 0.9


def test_is_allowed_missing_consumer_denied() -> None:
    registry = PolicyRegistry()
    assert registry.is_allowed(None, None) is False


def test_is_allowed_unknown_consumer_denied() -> None:
    registry = PolicyRegistry()
    assert registry.is_allowed("unknown", None) is False


def test_is_allowed_disabled_consumer_denied(tmp_path) -> None:
    path = _write_config(
        tmp_path,
        [{"consumer_id": "crm", "enabled": False, "api_keys": ["k1"]}],
    )
    registry = PolicyRegistry(config_path=path)
    assert registry.is_allowed("crm", "k1") is False


def test_is_allowed_key_match(tmp_path) -> None:
    path = _write_config(
        tmp_path,
        [{"consumer_id": "crm", "enabled": True, "api_keys": ["secret-key"]}],
    )
    registry = PolicyRegistry(config_path=path)
    assert registry.is_allowed("crm", "secret-key") is True
    assert registry.is_allowed("crm", "wrong-key") is False


def test_is_allowed_no_keys_means_open(tmp_path) -> None:
    path = _write_config(
        tmp_path,
        [{"consumer_id": "crm", "enabled": True}],
    )
    registry = PolicyRegistry(config_path=path)
    assert registry.is_allowed("crm", None) is True


def test_bad_config_keeps_last_known(tmp_path) -> None:
    path = _write_config(tmp_path, [{"consumer_id": "crm", "enabled": True}])
    registry = PolicyRegistry(config_path=path)
    assert registry.resolve("crm").consumer_id == "crm"
    # Corrupt the file; reload should keep the last known-good config.
    tmp_path.joinpath("policy.json").write_text("{broken", encoding="utf-8")
    registry._maybe_reload()
    assert registry.resolve("crm").consumer_id == "crm"


def test_masker_filters_enabled_pii_types() -> None:
    text = "Иван Иванов, email test@example.com, телефон +7 900 123 45 67"
    masker = PIIMasker(
        detectors=default_rule_detectors(),
        enabled_pii_types=frozenset({"EMAIL"}),
    )
    masked = masker.mask(text)
    assert "test@example.com" not in masked
    assert "Иван Иванов" in masked
    assert "+7 900 123 45 67" in masked


def test_masker_all_types_by_default() -> None:
    text = "Иван Иванов, email test@example.com"
    masker = PIIMasker(detectors=default_rule_detectors())
    masked = masker.mask(text)
    assert "test@example.com" not in masked
    assert "Иван Иванов" not in masked


def test_consumer_policy_defaults() -> None:
    policy = ConsumerPolicy(consumer_id="x")
    assert policy.enabled is True
    assert policy.allow_demasking is True
    assert policy.masking_mode == "typed_placeholder"
    assert policy.degradation == "fail_closed"
    assert "PERSON" in policy.enabled_pii_types


def test_excluded_pii_types_subtracted_from_enabled() -> None:
    policy = ConsumerPolicy(consumer_id="email-agent", excluded_pii_types=frozenset({"EMAIL"}))
    assert "EMAIL" not in policy.effective_pii_types
    assert "PERSON" in policy.effective_pii_types
    assert "PASSPORT" in policy.effective_pii_types


def test_excluded_pii_types_parsed_from_json(tmp_path) -> None:
    path = _write_config(
        tmp_path,
        [
            {
                "consumer_id": "email-agent",
                "enabled": True,
                "excluded_pii_types": ["EMAIL"],
                "allow_demasking": True,
            }
        ],
    )
    registry = PolicyRegistry(config_path=path)
    policy = registry.resolve("email-agent")
    assert "EMAIL" not in policy.effective_pii_types
    assert "PERSON" in policy.effective_pii_types


def test_masker_excludes_pii_types() -> None:
    text = "Иван Иванов, email test@example.com, телефон +7 900 123 45 67"
    masker = PIIMasker(
        detectors=default_rule_detectors(),
        enabled_pii_types=frozenset({"PERSON", "EMAIL", "PHONE"}) - frozenset({"EMAIL"}),
    )
    masked = masker.mask(text)
    assert "test@example.com" in masked
    assert "Иван Иванов" not in masked
    assert "+7 900 123 45 67" not in masked


def test_masker_synthetic_mode() -> None:
    text = "Иван Иванов, email test@example.com"
    masker = PIIMasker(
        detectors=default_rule_detectors(),
        masking_mode="synthetic",
    )
    masked = masker.mask(text)
    assert "test@example.com" not in masked
    assert "user@example.com_1" in masked
    assert "Иванов Иван Иванович_1" in masked


def test_masker_format_preserving_mode() -> None:
    text = "Иван Иванов, паспорт 45 10 123456"
    masker = PIIMasker(
        detectors=default_rule_detectors(),
        masking_mode="format_preserving",
    )
    masked = masker.mask(text)
    assert "Иван Иванов" not in masked
    assert "45 10 123456" not in masked
    # Length and separators preserved.
    assert "**** *****" in masked
    assert "** ** ******" in masked


def test_masker_rule_only_degradation() -> None:
    class _FailingMLDetector:
        def detect(self, text: str) -> list:
            raise RuntimeError("ml unavailable")

    text = "Иван Иванов, email test@example.com"
    masker = PIIMasker(
        detectors=default_rule_detectors(),
        ml_detectors=[_FailingMLDetector()],  # type: ignore[list-item]
        degradation="rule_only",
    )
    masked = masker.mask(text)
    # Rule-based masking still works despite ML failure.
    assert "Иван Иванов" not in masked
    assert "test@example.com" not in masked


def test_masker_fail_closed_raises_on_ml_error() -> None:
    class _FailingMLDetector:
        def detect(self, text: str) -> list:
            raise RuntimeError("ml unavailable")

    masker = PIIMasker(
        detectors=default_rule_detectors(),
        ml_detectors=[_FailingMLDetector()],  # type: ignore[list-item]
        degradation="fail_closed",
    )
    with pytest.raises(RuntimeError):
        masker.mask("Иван Иванов")


def test_custom_term_masks_exact_word_case_insensitive() -> None:
    """Custom terms are masked as exact words, case-insensitively."""
    from app.pii import CustomTermDetector

    detector = CustomTermDetector(["проект-альфа", "секрет"])
    text = "Проект-АЛЬФА запущен, секрет хранится, а секретный файл нет."
    matches = detector.detect(text)

    # "Проект-АЛЬФА" and "секрет" matched; "секретный" (substring) not matched.
    values = [m.value for m in matches]
    assert "Проект-АЛЬФА" in values
    assert "секрет" in values
    assert "секретный" not in values


def test_custom_term_masked_via_masker() -> None:
    """Custom terms are masked through PIIMasker even if not in enabled types."""
    from app.pii import CustomTermDetector

    masker = PIIMasker(
        detectors=default_rule_detectors() + (CustomTermDetector(["секрет"]),),
        custom_terms=("секрет",),
    )
    masked = masker.mask("Данные секрет и email test@example.com")
    assert "секрет" not in masked
    assert "__PII_CUSTOM_TERM_1__" in masked
    assert "test@example.com" not in masked


def test_custom_term_not_masked_without_terms() -> None:
    """Without custom_terms, the term is not masked."""
    masker = PIIMasker(detectors=default_rule_detectors())
    masked = masker.mask("Данные секрет")
    assert "секрет" in masked


def test_custom_term_overlap_wins() -> None:
    """Custom term (confidence 1.0) wins over overlapping real PII."""
    from app.pii import CustomTermDetector

    masker = PIIMasker(
        detectors=default_rule_detectors() + (CustomTermDetector(["Иванов"]),),
        custom_terms=("Иванов",),
    )
    masked = masker.mask("Иванов Иван")
    # "Иванов" is a custom term (confidence 1.0) and wins over PERSON.
    assert "__PII_CUSTOM_TERM_1__" in masked


def test_custom_term_round_trip() -> None:
    """Custom terms are restored on demasking (mapping round-trip)."""
    from app.pii import CustomTermDetector

    masker = PIIMasker(
        detectors=default_rule_detectors() + (CustomTermDetector(["секрет"]),),
        custom_terms=("секрет",),
    )
    text = "Данные секрет"
    masked = masker.mask(text)
    assert "секрет" not in masked
    # Mapping restores the original value.
    restored = masked
    for placeholder, value in masker.mapping.items():
        restored = restored.replace(placeholder, value)
    assert restored == text


def test_custom_term_parsed_from_config(tmp_path) -> None:
    """custom_terms are parsed from the JSON policy config."""
    path = _write_config(
        tmp_path,
        [
            {
                "consumer_id": "custom-agent",
                "custom_terms": ["секрет", "проект-альфа"],
            }
        ],
    )
    registry = PolicyRegistry(config_path=path)
    policy = registry.resolve("custom-agent")
    assert policy.custom_terms == ("секрет", "проект-альфа")


def test_custom_term_per_consumer_isolation(tmp_path) -> None:
    """Custom terms apply only to the configured consumer, not others."""
    path = _write_config(
        tmp_path,
        [
            {
                "consumer_id": "custom-agent",
                "custom_terms": ["секрет"],
            },
            {
                "consumer_id": "plain-agent",
            },
        ],
    )
    registry = PolicyRegistry(config_path=path)
    assert registry.resolve("custom-agent").custom_terms == ("секрет",)
    assert registry.resolve("plain-agent").custom_terms == ()
