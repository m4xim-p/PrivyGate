"""Model registry: maps client-facing model names to upstream endpoints.

The registry loads a JSON config (``config/models.json``) listing available
models with their upstream ``api_base`` and provider ``model`` name. It is
re-read on a TTL so models can be added/removed without redeploying.

The client sends only the model ``name`` (matching the config) plus its own
API key in ``X-Model-API-Key``. The proxy resolves the upstream endpoint and
forwards the client's key. Unknown model names are rejected.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("privygate.model_registry")


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Upstream configuration for a single model."""

    name: str
    api_base: str
    model: str


class ModelRegistry:
    """Loads model configs from a JSON file and resolves them by name."""

    def __init__(
        self,
        config_path: str | None = None,
        reload_interval: float = 30.0,
    ) -> None:
        self._config_path = config_path
        self._reload_interval = reload_interval
        self._lock = threading.Lock()
        self._last_load = 0.0
        self._models: dict[str, ModelConfig] = {}
        self._load()

    def _load(self) -> None:
        if not self._config_path:
            return
        path = Path(self._config_path)
        if not path.exists():
            logger.warning("models_config_missing path=%s", self._config_path)
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "models_config_load_failed path=%s error_type=%s",
                self._config_path,
                type(exc).__name__,
            )
            return
        models: dict[str, ModelConfig] = {}
        for item in data.get("models", []):
            name = item.get("name")
            api_base = item.get("api_base")
            model = item.get("model", name)
            if not name or not api_base:
                continue
            models[name] = ModelConfig(
                name=name,
                api_base=str(api_base).rstrip("/"),
                model=str(model),
            )
        with self._lock:
            self._models = models
            self._last_load = time.monotonic()
        logger.info("models_config_loaded path=%s count=%d", self._config_path, len(models))

    def _maybe_reload(self) -> None:
        if not self._config_path:
            return
        if time.monotonic() - self._last_load < self._reload_interval:
            return
        self._load()

    def resolve(self, model_name: str) -> ModelConfig | None:
        """Return the model config for a name, or None if unknown."""
        self._maybe_reload()
        with self._lock:
            return self._models.get(model_name)

    def names(self) -> list[str]:
        self._maybe_reload()
        with self._lock:
            return sorted(self._models)


def default_models_path() -> str | None:
    """Return the default models config path from env, if set."""
    return os.getenv("MODELS_CONFIG_PATH")
