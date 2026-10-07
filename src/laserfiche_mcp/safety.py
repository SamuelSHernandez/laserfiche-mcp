"""The tool error boundary: nothing leaves a tool as a raw exception or a secret.

Every registered tool is wrapped by :func:`safe_tool` (see ``server._register_one``),
innermost, under ``tool_logger``. Tools still handle the failures they expect and
return precise structured errors; this is the guarantee for everything they
don't:

* any ``Exception`` that escapes becomes a structured ``mode: "error"`` response
  (canonical ``kind``, a subkind, ``request_id``) via :func:`errors.unexpected_error`
  instead of FastMCP's opaque ``Error executing tool <name>: <raw text>``;
* the full traceback goes to the server log under the same ``request_id``, never to
  the model;
* every error response is scrubbed of the configured secrets (password, client
  secret, tokens, signing key) as defense in depth, in case a server reply or an
  exception text echoes one back.

``BaseException`` (cancellation, ``KeyboardInterrupt``, ``SystemExit``) is
deliberately NOT caught: shutting down and cancelling a request must keep working.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from .errors import unexpected_error
from .observability import get_request_id_or_new

logger = logging.getLogger("laserfiche_mcp.safety")

ToolFn = Callable[..., Awaitable[Any]]

# Secrets shorter than this are not scrubbed: replacing a 1-3 character value would
# mangle ordinary text (and a secret that short is not protected by hiding it anyway).
_MIN_SECRET_LENGTH = 4
REDACTED_SECRET = "<redacted>"


def _configured_secrets() -> list[str]:
    """Secret values from the active settings, longest first (so overlaps scrub cleanly)."""
    try:
        from ._app import get_settings  # noqa: PLC0415 — avoid an import cycle at load

        settings = get_settings()
    except Exception:  # noqa: BLE001 — no valid settings (early failure, unit tests)
        return []
    values: list[str] = []
    for name in (
        "password",
        "client_secret",
        "cloud_access_key",
        "cloud_service_principal_key",
        "http_auth_token",
        "confirmation_secret",
    ):
        secret = getattr(settings, name, None)
        raw = secret.get_secret_value() if secret is not None else None
        if raw and len(raw) >= _MIN_SECRET_LENGTH:
            values.append(raw)
    return sorted(set(values), key=len, reverse=True)


def scrub_secrets(obj: Any, secrets: list[str] | None = None) -> Any:
    """Return ``obj`` with every configured secret replaced by ``<redacted>``.

    Walks dicts, lists and tuples; only strings are rewritten. Never mutates.
    """
    values = _configured_secrets() if secrets is None else secrets
    if not values:
        return obj
    return _scrub(obj, values)


def _scrub(obj: Any, values: list[str]) -> Any:
    if isinstance(obj, str):
        for secret in values:
            if secret in obj:
                obj = obj.replace(secret, REDACTED_SECRET)
        return obj
    if isinstance(obj, dict):
        return {k: _scrub(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, values) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub(v, values) for v in obj)
    return obj


def safe_tool(fn: ToolFn) -> ToolFn:
    """Wrap a tool so it can never raise an ``Exception`` or return a secret in an error.

    Idempotent. Apply it INSIDE ``tool_logger`` so the logger sees (and counts) the
    structured error and its ``request_id``.
    """
    if getattr(fn, "_lf_safe_wrapped", False):
        return fn

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            request_id = get_request_id_or_new()
            logger.error(
                "unhandled %s in tool %s (request_id=%s)",
                type(exc).__name__,
                fn.__name__,
                request_id,
                exc_info=exc,
            )
            entry_id = kwargs.get("entry_id")
            return scrub_secrets(
                unexpected_error(
                    fn.__name__,
                    exc,
                    entry_id=entry_id if isinstance(entry_id, int) else None,
                )
            )
        if isinstance(result, dict) and result.get("mode") == "error":
            return scrub_secrets(result)
        return result

    wrapper._lf_safe_wrapped = True  # type: ignore[attr-defined]
    return wrapper
