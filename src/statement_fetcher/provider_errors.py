from __future__ import annotations

from typing import Any


class ProviderAPIError(RuntimeError):
    """Base error for any statement provider (Plaid, Yodlee, ...).

    sync.py handles Plaid and Yodlee failures identically wherever possible, so both
    providers' error types share this shape (status_code/retriable/details) rather
    than each sprouting its own incompatible attributes.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retriable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable
        self.details = details or {}
