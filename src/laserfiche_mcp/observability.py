"""Observability primitives: redaction, request-id propagation, structured per-tool-call logging.

Three responsibilities, deliberately kept in one module so the dependency
graph stays a forest (errors.py → observability.py; tools/* → observability.py
via the decorator wired in server.py; nothing imports back into the package
from here at import time).

1. ``redact(obj, *, host=None, repo_id=None)`` — single redaction helper.
   Walks dicts/lists/tuples, replaces values of credential-like keys with
   ``"<redacted>"``, and scrubs ``host`` / ``repo_id`` substrings inside
   strings (used by retry-warning logs that include URLs).

2. ``request_id`` ``ContextVar`` + ``get_request_id() / get_request_id_or_new()``
   helpers. ``tool_logger`` sets the var on entry; ``errors.classify_lf_error``
   reads it so the same ID appears in both the per-call log line and the
   error response returned to the agent. Outside a tool call (direct test
   invocation, ad-hoc CLI use) the helpers fall back to a fresh UUID4.

3. ``tool_logger(fn)`` decorator + ``configure_logging(level, format_)`` —
   emits exactly one structured log event per tool call, with optional
   JSON output (``LF_LOG_FORMAT=json``) so operators piping to ``jq`` or
   forwarding to Datadog get a stable shape.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any

logger = logging.getLogger("laserfiche_mcp.tools")

# --- Request-ID propagation ---------------------------------------------------

_request_id_var: ContextVar[str | None] = ContextVar(
    "laserfiche_mcp_request_id", default=None
)


def get_request_id() -> str | None:
    """Return the request ID for the current tool call, or ``None`` if unset."""
    return _request_id_var.get()


def get_request_id_or_new() -> str:
    """Return the current request ID, or a fresh UUID4 if none is set.

    Error classifiers call this so the request_id surfaced on a
    ``mode:error`` response matches the per-call log line emitted by the
    decorator. When invoked outside ``tool_logger`` (direct test calls,
    ad-hoc scripts) the fresh UUID keeps the field non-null.
    """
    rid = _request_id_var.get()
    if rid is not None:
        return rid
    return str(uuid.uuid4())


def set_request_id(value: str) -> object:
    """Bind ``value`` as the current request ID and return the reset token.

    Mirrors ``ContextVar.set`` — the caller must pass the returned token to
    :func:`reset_request_id` (typically in a ``finally``).
    """
    return _request_id_var.set(value)


def reset_request_id(token: object) -> None:
    """Restore the request ID to its previous value using the token from :func:`set_request_id`."""
    # ContextVar.reset accepts the token returned by .set(); typed as
    # opaque here to keep callers from importing contextvars.Token.
    _request_id_var.reset(token)  # type: ignore[arg-type]


# --- Redaction ----------------------------------------------------------------

# Keys whose value is always replaced. Case-insensitive match against the
# dict key. ``confirmation_token`` is technically not a credential but acts
# as a verifier secret — including it is conservative.
_REDACT_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "client_secret",
        "api_key",
        "token",
        "authorization",
        "cookie",
        "x-api-key",
        "lf_password",
        "lf_client_secret",
        "lf_api_key",
        "confirmation_token",
    }
)

REDACTED = "<redacted>"
REDACTED_HOST = "<repo_host>"
REDACTED_REPO = "<repo_id>"


def redact(
    obj: Any,
    *,
    host: str | None = None,
    repo_id: str | None = None,
) -> Any:
    """Return a deep-copied, redacted version of ``obj`` safe for logging.

    Rules:
        * Dict values whose key matches the credential deny-list
          (case-insensitive) are replaced with ``"<redacted>"``.
        * Strings have ``host`` and ``repo_id`` substrings rewritten to
          ``"<repo_host>"`` / ``"<repo_id>"`` (whole-segment match for
          repo_id so it doesn't mangle unrelated tokens that happen to
          share the prefix).
        * Recurses into dicts, lists, and tuples.
        * Other scalars (int, bool, None, bytes, pydantic ``SecretStr``)
          pass through unchanged — ``SecretStr`` redacts itself in
          ``repr()`` already.

    The input is never mutated.
    """
    return _redact_inner(obj, host=host, repo_id=repo_id)


def _redact_inner(obj: Any, *, host: str | None, repo_id: str | None) -> Any:
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _REDACT_KEYS:
                out[k] = REDACTED
            else:
                out[k] = _redact_inner(v, host=host, repo_id=repo_id)
        return out
    if isinstance(obj, list):
        return [_redact_inner(v, host=host, repo_id=repo_id) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_redact_inner(v, host=host, repo_id=repo_id) for v in obj)
    if isinstance(obj, str):
        return _redact_string(obj, host=host, repo_id=repo_id)
    return obj


def _redact_string(s: str, *, host: str | None, repo_id: str | None) -> str:
    out = s
    if host:
        out = out.replace(host, REDACTED_HOST)
    if repo_id:
        # Match on word-ish boundaries so the repo_id substring inside a
        # URL path gets replaced but a longer identifier that merely starts
        # with the repo_id does not.
        pattern = rf"(?<![A-Za-z0-9_-]){re.escape(repo_id)}(?![A-Za-z0-9_-])"
        out = re.sub(pattern, REDACTED_REPO, out)
    return out


def _host_and_repo_from_settings() -> tuple[str | None, str | None]:
    """Read the configured host + repo_id without raising at import time.

    Used by ``tool_logger`` and the existing log lines in ``auth.py`` /
    ``client/_core.py``. Returns ``(None, None)`` when settings haven't
    been loaded yet (early CLI failure path, ad-hoc test imports).
    """
    try:
        from ._app import get_settings  # local import to avoid cycles

        settings = get_settings()
    except Exception:
        return (None, None)
    host: str | None = None
    if settings.repo_api_url is not None:
        host = settings.repo_api_url.host
    return (host, settings.repository_id)


# --- Per-tool-call logging decorator ------------------------------------------

ToolFn = Callable[..., Awaitable[dict[str, Any]]]


def tool_logger(fn: ToolFn) -> ToolFn:
    """Wrap an MCP tool function so each call emits one structured log event.

    Behavior:
        * Generates a UUID4 ``request_id`` and stores it in the
          module-level ``ContextVar``. Error classifiers read it via
          :func:`get_request_id_or_new` so the ID in the log line matches
          the ID in the agent-visible error response.
        * Times the call with ``time.perf_counter``.
        * On success (result dict without ``mode:"error"``) emits an
          ``outcome="ok"`` event at INFO.
        * On structured error (``mode:"error"``) emits ``outcome="error"``
          at WARNING with ``error_kind``, ``error_subkind``, and
          ``upstream_trace_id`` lifted out of the result.
        * On uncaught exception, emits ``outcome="exception"`` at WARNING
          and re-raises.

    The wrapped function's positional args and keyword args are redacted
    via :func:`redact` before logging.

    Decorator is idempotent — applying twice is a no-op, so registry
    code can wrap every tool without worrying about a tool that's
    already been wrapped during import.
    """
    if getattr(fn, "_lf_tool_logger_wrapped", False):
        return fn

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        token = set_request_id(request_id)
        start = time.perf_counter()
        host, repo_id = _host_and_repo_from_settings()
        args_redacted = redact(
            {"args": list(args), "kwargs": kwargs} if args else kwargs,
            host=host,
            repo_id=repo_id,
        )
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000.0
            _emit_event(
                tool=fn.__name__,
                request_id=request_id,
                outcome="exception",
                duration_ms=duration_ms,
                args_redacted=args_redacted,
                error_kind="upstream_unavailable",
                error_subkind="uncaught_exception",
                error_message=str(exc)[:300],
            )
            raise
        finally:
            reset_request_id(token)

        duration_ms = (time.perf_counter() - start) * 1000.0
        if isinstance(result, dict) and result.get("mode") == "error":
            _emit_event(
                tool=fn.__name__,
                request_id=request_id,
                outcome="error",
                duration_ms=duration_ms,
                args_redacted=args_redacted,
                error_kind=result.get("kind"),
                error_subkind=result.get("error"),
                upstream_trace_id=result.get("upstream_trace_id"),
            )
            # Defense-in-depth: pre-server guards that build error dicts
            # without going through ``classify_lf_error`` may not have set
            # request_id; backfill it so the agent sees the same ID we
            # logged.
            result.setdefault("request_id", request_id)
        else:
            _emit_event(
                tool=fn.__name__,
                request_id=request_id,
                outcome="ok",
                duration_ms=duration_ms,
                args_redacted=args_redacted,
            )
        return result

    wrapper._lf_tool_logger_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def _emit_event(
    *,
    tool: str,
    request_id: str,
    outcome: str,
    duration_ms: float,
    args_redacted: Any,
    **extras: Any,
) -> None:
    """Emit one per-call event via the package logger.

    The formatter installed by :func:`configure_logging` decides whether
    the line is human-readable or JSON; the ``lf_event`` extras dict
    carries the structured payload either way.
    """
    event: dict[str, Any] = {
        "tool": tool,
        "request_id": request_id,
        "outcome": outcome,
        "duration_ms": round(duration_ms, 2),
        "args": args_redacted,
    }
    for k, v in extras.items():
        if v is not None:
            event[k] = v
    level = logging.INFO if outcome == "ok" else logging.WARNING
    logger.log(
        level,
        "tool_call %s %s %.2fms",
        tool,
        outcome,
        duration_ms,
        extra={"lf_event": event},
    )


# --- Log formatting -----------------------------------------------------------


class JsonLogFormatter(logging.Formatter):
    """Emit one JSON object per log record.

    Structured per-tool-call events (set via the ``lf_event`` extra by
    :func:`tool_logger`) get hoisted to a top-level ``event`` key. Plain
    log records still get a stable wrapper shape so log forwarders see
    one schema regardless of source.
    """

    default_msec_format = "%s.%03dZ"

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        lf_event = getattr(record, "lf_event", None)
        if lf_event is not None:
            payload["event"] = lf_event
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str = "INFO", format_: str = "text") -> None:
    """Install the root logger handler matching the requested format.

    Called from ``cli.main()`` after settings load. Removes any existing
    handlers first so a re-invocation (e.g. in tests) doesn't cascade
    duplicate output.

    Args:
        level: A Python logging level name (``DEBUG``, ``INFO``, ...).
        format_: ``"text"`` (default, human-readable) or ``"json"``
            (one JSON object per line, suitable for jq / log aggregators).
    """
    fmt = (format_ or "text").lower()
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(level.upper())
