"""Bind nested official HTTP to an owning recovery action, not a second loop.

The scope is thread/context-local. Callers may copy it into a worker; each
wire request (including a redirect) must consume the same declared budget.
No credential, request body or provider response is retained here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import time
from urllib.error import HTTPError

from .contracts import Usage
from .strategies import RecoveryError


_remote_context = ContextVar("openthesis_remote_recovery_context", default=None)


@contextmanager
def remote_request_scope(context):
    token = _remote_context.set(context)
    try:
        yield
    finally:
        _remote_context.reset(token)


def current_remote_context():
    return _remote_context.get()


def before_remote_request():
    context = current_remote_context()
    if context is not None:
        context.consume(Usage(calls=1))


def retry_after_seconds(value, *, now=None):
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        seconds = float(text)
    except ValueError:
        try:
            date = parsedate_to_datetime(text)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return max(0.0, seconds) if math.isfinite(seconds) else 0.0


def managed_remote_failure(error):
    if isinstance(error, HTTPError):
        status = error.code
        transient = status in {408, 425, 429, 500, 502, 503, 504}
        return RecoveryError(f"REMOTE_HTTP_{status}", transient=transient,
            retry_after=retry_after_seconds(error.headers.get("Retry-After", "") if error.headers else ""),
            needs_action="configure_official_source" if status in {401, 403} else "retry_official_source")
    return RecoveryError("REMOTE_TRANSPORT_FAILED", transient=True, needs_action="retry_official_source")
