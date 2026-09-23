"""PIIMaskingEngine: thin abstraction over PIIMasker for /process."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from app.pii import (
    DEFAULT_MASKING_CONFIDENCE,
    CustomTermDetector,
    PIIDetector,
    PIIMasker,
)
from app.policy import ConsumerPolicy


class PIIMaskingEngine:
    """Creates a fresh PIIMasker per request and runs masking in a worker thread.

    Holds an immutable detector profile. Returns only the masked text and safe
    diagnostics (count, types). Never returns or stores the mapping.
    """

    def __init__(
        self,
        detectors: Sequence[PIIDetector],
        *,
        max_workers: int = 32,
        default_policy: ConsumerPolicy | None = None,
        ml_detectors: Sequence[PIIDetector] | None = None,
    ) -> None:
        self._detectors = tuple(detectors)
        self._ml_detectors = tuple(ml_detectors or ())
        self._default_policy = default_policy or ConsumerPolicy(
            consumer_id="alfasonar"
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="pii-mask"
        )

    async def mask(
        self, text: str, policy: ConsumerPolicy | None = None
    ) -> tuple[str, int, list[str]]:
        profile = policy or self._default_policy

        def _run() -> tuple[str, int, list[str]]:
            detectors = self._detectors
            if profile.custom_terms:
                detectors = detectors + (
                    CustomTermDetector(profile.custom_terms),
                )
            masker = PIIMasker(
                detectors=detectors,
                min_confidence=(
                    profile.min_confidence
                    if profile.min_confidence is not None
                    else DEFAULT_MASKING_CONFIDENCE
                ),
                enabled_pii_types=profile.effective_pii_types,
                masking_mode=profile.masking_mode,
                ml_detectors=self._ml_detectors,
                degradation=profile.degradation,
                require_card_for_pin=profile.require_card_for_pin,
                custom_terms=profile.custom_terms,
            )
            masked = masker.mask(text)
            return masked, len(masker.mapping), masker.pii_types

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _run)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
