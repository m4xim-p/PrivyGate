"""Tests for Consumer Policy Registry (ADR-0004)."""

from __future__ import annotations

import json

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


def test_is_allowed_process_always_allowed() -> None:
    registry = PolicyRegistry()
    assert registry.is_allowed(None, None) is True


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
