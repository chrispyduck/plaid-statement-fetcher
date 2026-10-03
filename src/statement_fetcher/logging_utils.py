from __future__ import annotations

import logging
from contextvars import ContextVar

# Holds the current log context (e.g. job_id, institution) for whichever thread is
# running it. Each background job (sync/refresh) runs on its own dedicated thread, so
# this naturally stays isolated per job without any explicit cleanup: a fresh thread
# starts with an empty context, and `set_log_context` below replaces it as the job
# progresses from "whole job" scope down to "this linked item" scope.
_log_context: ContextVar[dict[str, str] | None] = ContextVar("_log_context", default=None)


def set_log_context(**fields: str | None) -> None:
    """Replace the active log context for the current thread.

    Every subsequent log line on this thread -- including ones emitted by httpx and
    plaid_api, which have no idea a job or item exists -- gets tagged with this
    context by `ContextualFormatter`, so raw logs can be grepped by job_id or
    institution without having to manually correlate nearby timestamps.
    """
    _log_context.set({key: value for key, value in fields.items() if value})


class ContextualFormatter(logging.Formatter):
    """Formatter that prefixes each line with the active log context, e.g.
    "[job_id=... institution=...] Plaid request failed ...".
    """

    def format(self, record: logging.LogRecord) -> str:
        context = _log_context.get()
        record.context = (
            f"[{' '.join(f'{key}={value}' for key, value in context.items())}] " if context else ""
        )
        return super().format(record)
