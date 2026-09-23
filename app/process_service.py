"""ProcessService: orchestrator for the /process contract."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from app.errors import ConflictError, ProcessError, TooManyRequestsError
from app.pii_engine import PIIMaskingEngine
from app.policy import ConsumerPolicy
from app.process_store import ProcessSession, ProcessStore, SessionState

logger = logging.getLogger("privygate.process_service")


def _payload_fingerprint(payload: str) -> tuple[str, int]:
    encoded = payload.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return digest, len(encoded)


def estimate_tokens(text: str) -> int:
    """Approximate token count without a heavy tokenizer.

    A rough heuristic: ~4 characters per token for mixed Russian/English text,
    which is close to common BPE tokenizers. Used only for admission control,
    not for exact token accounting.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _consume_exception(future: asyncio.Future) -> None:
    """Retrieve a stored exception so it is not reported as unhandled."""
    if future.cancelled():
        return
    future.exception()


class ProcessService:
    """Orchestrates masking/demasking without owning regex, tokenizer or masking.

    Delegates detection to PIIMaskingEngine and state to ProcessStore. Does not
    contain regex, does not call a tokenizer, does not implement masking itself.
    """

    def __init__(
        self,
        engine: PIIMaskingEngine,
        store: ProcessStore,
        *,
        waiter_timeout: float = 5.0,
        max_payload_bytes: int = 400_000,
        max_estimated_tokens: int = 100_000,
    ) -> None:
        self._engine = engine
        self.store = store
        self._waiter_timeout = waiter_timeout
        self._max_payload_bytes = max_payload_bytes
        self._max_estimated_tokens = max_estimated_tokens

    async def process(
        self,
        payload: str,
        payload_id: str,
        policy: ConsumerPolicy | None = None,
    ) -> str:
        from app.errors import PayloadTooLargeError

        payload_bytes = len(payload.encode("utf-8"))
        if payload_bytes > self._max_payload_bytes:
            raise PayloadTooLargeError(
                f"payload too large: maximum payload size is "
                f"{self._max_payload_bytes} bytes, but you sent {payload_bytes} bytes. "
                f"Please reduce the length of the payload."
            )
        token_count = estimate_tokens(payload)
        if token_count > self._max_estimated_tokens:
            raise PayloadTooLargeError(
                f"payload too large: maximum context length is "
                f"{self._max_estimated_tokens} tokens, but you requested "
                f"{token_count} tokens. Please reduce the length of the payload."
            )

        fingerprint, length = _payload_fingerprint(payload)
        pending = await self.store.get_or_create_pending(payload_id, length, fingerprint)

        if not pending.is_new:
            if pending.conflict:
                raise ConflictError("payload_id already used with a different payload")
            return await self._await_pending(payload_id, pending.future, fingerprint, length)

        try:
            masked, pii_count, pii_types = await self._engine.mask(payload, policy)
            await self.store.publish_active(
                payload_id, payload, masked, pii_count, pii_types
            )
            pending.future.set_result(masked)
            logger.info(
                "process_masked payload_id=%s pii_count=%d pii_types=%s",
                payload_id,
                pii_count,
                ",".join(pii_types) or "none",
            )
            return masked
        except ProcessError:
            await self.store.release_pending(payload_id, pending.future)
            raise
        except BaseException as exc:
            await self.store.release_pending(payload_id, pending.future)
            if not pending.future.done():
                pending.future.set_exception(exc)
                pending.future.add_done_callback(_consume_exception)
            # Fail-closed: an unavailable detector must not return raw text as a
            # mask. Surface a controlled 5xx without leaking the exception text.
            raise ProcessError("processing failed") from None

    async def _await_pending(
        self,
        payload_id: str,
        future: asyncio.Future[str],
        fingerprint: str,
        length: int,
    ) -> str:
        session = await self.store.get(payload_id)
        if session is not None:
            return self._resolve_existing(session, fingerprint, length, payload_id)

        try:
            result = await asyncio.wait_for(
                asyncio.shield(future), timeout=self._waiter_timeout
            )
            return result
        except TimeoutError:
            raise TooManyRequestsError("waiter timed out", retry_after=1.0) from None

    def _resolve_existing(
        self,
        session: ProcessSession,
        fingerprint: str,
        length: int,
        payload_id: str,
    ) -> str:
        original_fp, original_len = _payload_fingerprint(session.original)
        masked_fp, masked_len = _payload_fingerprint(session.masked)

        if fingerprint == original_fp and length == original_len:
            return session.masked
        if fingerprint == masked_fp and length == masked_len:
            if session.state is SessionState.ACTIVE:
                asyncio.get_running_loop().create_task(self.store.complete(payload_id))
            return session.original
        raise ConflictError("payload_id already used with a different payload")
