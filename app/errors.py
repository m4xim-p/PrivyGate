"""Domain errors for the /process contract."""

from __future__ import annotations


class ProcessError(Exception):
    """Base error for the /process contract with an HTTP status code."""

    status_code: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationError(ProcessError):
    """Malformed request: missing field or wrong type."""

    status_code = 422


class ConflictError(ProcessError):
    """Existing payload_id used with an unknown payload."""

    status_code = 409


class PayloadTooLargeError(ProcessError):
    """Payload exceeds the configured byte/token limit."""

    status_code = 413


class TooManyRequestsError(ProcessError):
    """Admission limit exhausted; caller should retry after Retry-After."""

    status_code = 429

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GoneError(ProcessError):
    """payload_id expired and is no longer available."""

    status_code = 410
