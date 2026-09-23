"""Consumer Policy Registry (ADR-0004).

Selects a masking profile per consumer identity and enforces an allowlist for
the product API. The default ``alfasonar`` profile is used for the evaluation
``/process`` endpoint, which arrives without auth headers.

This module lives in the application-services layer: it knows nothing about
FastAPI, HTTP or ProcessStore. It only resolves policies and checks allowlist.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Literal

MaskingMode = Literal["typed_placeholder", "synthetic", "format_preserving"]
DegradationMode = Literal["fail_closed", "rule_only"]

# All mandatory PII categories from the track specification.
_ALL_PII_TYPES = frozenset(
    {
        "PERSON",
        "DATE_OF_BIRTH",
        "BIRTH_PLACE",
        "PASSPORT",
        "CITIZENSHIP",
        "PASSPORT_AUTHORITY",
        "PASSPORT_UNIT_CODE",
        "PASSPORT_ISSUE_DATE",
        "DRIVING_LICENSE",
        "ADDRESS",
        "EMAIL",
        "PHONE",
        "INN",
        "CARD",
        "CVV",
        "PIN",
        "CARD_HOLDER",
    }
)


@dataclass(frozen=True)
class ConsumerPolicy:
    """Masking profile for a single consumer system.

    The minimal fields cover criterion 3.4 (enabled PII types, demasking
    permission, on/off). The remaining fields are optional extensions.

    ``enabled_pii_types`` is the set of PII types to mask. ``excluded_pii_types``
    is subtracted from it, so a consumer can express "mask everything except
    EMAIL" without listing all other types.
    """

    consumer_id: str
    enabled: bool = True
    enabled_pii_types: frozenset[str] = field(default_factory=lambda: _ALL_PII_TYPES)
    excluded_pii_types: frozenset[str] = frozenset()
    allow_demasking: bool = True
    min_confidence: float | None = None
    masking_mode: MaskingMode = "typed_placeholder"
    degradation: DegradationMode = "fail_closed"

    @property
    def effective_pii_types(self) -> frozenset[str]:
        """PII types actually masked after applying exclusions."""
        return self.enabled_pii_types - self.excluded_pii_types


@dataclass(frozen=True)
class _ConsumerEntry:
    """Internal registry entry: policy plus allowlist credentials."""

    policy: ConsumerPolicy
    api_keys: frozenset[str] = frozenset()


class PolicyRegistry:
    """Loads consumer policies from a JSON file and resolves them by identity.

    The JSON file is re-read on a TTL so settings can change without redeploying
    or restarting the service. ``resolve`` returns the default ``alfasonar``
    profile for ``/process`` and for unknown/unauthenticated product calls.
    """

    def __init__(
        self,
        *,
        config_path: str | None = None,
        reload_interval: float = 30.0,
        default_policy: ConsumerPolicy | None = None,
    ) -> None:
        self._config_path = config_path
        self._reload_interval = reload_interval
        self._default_policy = default_policy or ConsumerPolicy(
            consumer_id="alfasonar"
        )
        self._entries: dict[str, _ConsumerEntry] = {}
        self._lock = threading.Lock()
        self._last_load = 0.0
        self._load()

    def _load(self) -> None:
        if not self._config_path or not os.path.exists(self._config_path):
            return
        try:
            with open(self._config_path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            # Keep the last known-good config on a bad reload.
            return
        entries: dict[str, _ConsumerEntry] = {}
        for item in raw.get("consumers", []):
            policy = self._parse_policy(item)
            keys = frozenset(item.get("api_keys", []))
            entries[policy.consumer_id] = _ConsumerEntry(policy=policy, api_keys=keys)
        with self._lock:
            self._entries = entries
            self._last_load = _now()

    def _maybe_reload(self) -> None:
        if not self._config_path:
            return
        if _now() - self._last_load < self._reload_interval:
            return
        self._load()

    @staticmethod
    def _parse_policy(item: dict) -> ConsumerPolicy:
        enabled_types = item.get("enabled_pii_types")
        excluded_types = item.get("excluded_pii_types")
        return ConsumerPolicy(
            consumer_id=str(item["consumer_id"]),
            enabled=bool(item.get("enabled", True)),
            enabled_pii_types=(
                frozenset(enabled_types) if enabled_types is not None else _ALL_PII_TYPES
            ),
            excluded_pii_types=(
                frozenset(excluded_types) if excluded_types is not None else frozenset()
            ),
            allow_demasking=bool(item.get("allow_demasking", True)),
            min_confidence=item.get("min_confidence"),
            masking_mode=item.get("masking_mode", "typed_placeholder"),
            degradation=item.get("degradation", "fail_closed"),
        )

    def resolve(self, consumer_id: str | None) -> ConsumerPolicy:
        """Return the policy for a consumer, or the default ``alfasonar`` profile."""
        self._maybe_reload()
        if consumer_id is None:
            return self._default_policy
        with self._lock:
            entry = self._entries.get(consumer_id)
        if entry is None:
            return self._default_policy
        return entry.policy

    def is_allowed(self, consumer_id: str | None, api_key: str | None) -> bool:
        """Allowlist check for the product API.

        A consumer is allowed when it is registered and its API key matches.
        A missing consumer identity is denied: the product API requires an
        explicit ``X-Consumer-ID``. The evaluation ``/process`` endpoint does
        not call this method and always uses the default ``alfasonar`` profile.
        """
        self._maybe_reload()
        if consumer_id is None:
            return False
        with self._lock:
            entry = self._entries.get(consumer_id)
        if entry is None:
            return False
        if not entry.policy.enabled:
            return False
        if not entry.api_keys:
            return True
        return api_key is not None and api_key in entry.api_keys

    def close(self) -> None:
        pass


def _now() -> float:
    import time

    return time.monotonic()
