"""Exception hierarchy and HTTP-error → tool-error classification.

This module owns two responsibilities:

1. ``LaserficheError`` — raised by the HTTP client when the Repository API
   returns a non-2xx response or an unparseable payload. Carries the parsed
   ProblemDetails body so callers can extract ``errorCode``, ``title``, etc.
   without re-parsing the message string.

2. ``classify_lf_error`` — converts a ``LaserficheError`` into the
   structured ``{"mode": "error", ...}`` response that tool functions
   return to the calling LLM. Maps HTTP status codes + Laserfiche
   ``errorCode`` values onto a stable taxonomy of subkinds (the
   ``error`` field) grouped under five canonical kinds (the ``kind``
   field): ``not_found``, ``permission_denied``, ``rate_limited``,
   ``invalid_input``, ``upstream_unavailable``.

   The five-kind taxonomy is part of the public error contract — see
   docs/error-contract.md. Subkinds preserve the actionable detail of
   the underlying failure; LLM agents can branch on either granularity.
"""

from __future__ import annotations

from typing import Any

from .observability import get_request_id_or_new


class LaserficheError(Exception):
    """Raised when the Repository API returns an error or unexpected response.

    ``detail`` carries the parsed response body (dict if JSON, str if
    plaintext) so callers can extract ``errorCode``, ``title``, etc.
    without re-parsing the message string.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        detail: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


# Known Laserfiche-specific error codes (the ``errorCode`` field on
# ProblemDetails responses, distinct from HTTP status).
_LF_ERROR_CODE_AUTH_INVALID = 9010
_LF_ERROR_CODE_LFDS_UNREACHABLE = 9528  # Misleading message; usually = bad creds.
_LF_ERROR_CODE_REQUIRED_FIELD_A = 9039
_LF_ERROR_CODE_REQUIRED_FIELD_B = 9066


def lf_error_detail(exc: LaserficheError) -> dict[str, Any]:
    """Pull errorCode/title/instance out of a LaserficheError, if present."""
    d = exc.detail
    if isinstance(d, dict):
        # ProblemDetails fields sometimes live at top level, sometimes
        # nested under 'error' (the older Edoc routes use the nested form).
        inner = d.get("error") if isinstance(d.get("error"), dict) else None
        return {**(inner or {}), **{k: v for k, v in d.items() if k != "error"}}
    return {}


# Subkind → kind mapping. The five canonical ToolErrorKind values
# per the v2 error contract. Subkinds preserve the actionable signal
# of the granular taxonomy; kind lets agents branch on category.
_SUBKIND_TO_KIND: dict[str, str] = {
    # permission_denied
    "auth_failed": "permission_denied",
    "path_not_allowed": "permission_denied",
    "path_traversal_blocked": "permission_denied",
    "tool_not_allowed": "permission_denied",
    # not_found
    "not_found": "not_found",
    # rate_limited
    "rate_limited": "rate_limited",
    # invalid_input
    "required_field_missing": "invalid_input",
    "missing_required_fields": "invalid_input",
    "invalid_confirmation_token": "invalid_input",
    "exceeds_batch_cap": "invalid_input",
    "audit_reason_required": "invalid_input",
    "page_range_required": "invalid_input",
    "invalid_page_range": "invalid_input",
    "invalid_name": "invalid_input",
    "invalid_field_name": "invalid_input",
    "invalid_field_value": "invalid_input",
    "invalid_template_name": "invalid_input",
    "invalid_tag_name": "invalid_input",
    "invalid_link_type": "invalid_input",
    "unsupported_media_type": "invalid_input",
    "file_not_found": "invalid_input",
    "size_exceeds_cap": "invalid_input",
    "expected_folder_got_document": "invalid_input",
    "bad_query_syntax": "invalid_input",
    # upstream_unavailable
    "server_error": "upstream_unavailable",
    "method_not_allowed": "upstream_unavailable",
    "endpoint_disabled": "upstream_unavailable",
}


def kind_for_subkind(subkind: str) -> str:
    """Return the canonical ToolErrorKind for a subkind. Public helper."""
    return _SUBKIND_TO_KIND.get(subkind, "upstream_unavailable")


# Each entry maps a (slug, reason) onto its identifying signal. The first
# matching rule wins, in order: Laserfiche-specific ``errorCode`` overrides
# HTTP status. Falls through to the generic 5xx / unknown branch at the bottom
# of ``classify_lf_error`` if nothing matches.
_AUTH_REASON = (
    "Laserfiche rejected the credentials. Verify LF_USERNAME and "
    "LF_PASSWORD; on self-hosted, 9528 ('LFDS unreachable') is "
    "misleadingly worded and most often also means bad creds."
)
_REQUIRED_FIELD_REASON = (
    "The repository has one or more required fields that aren't "
    "set on this entry. Call list_field_definitions and supply "
    "isRequired=true fields via the tool's `fields` parameter."
)
_HTTP_AUTH_REASON = (
    "HTTP 401/403 — credentials or permissions reject this "
    "operation. Confirm the service account has rights on the "
    "target path."
)
_HTTP_405_REASON = (
    "HTTP 405 — the URL didn't match a route with this HTTP "
    "method. Usually an MCP routing bug or a build that doesn't "
    "expose the endpoint."
)
_HTTP_415_REASON = (
    "HTTP 415 — the server expected a Content-Type the request "
    "didn't supply. Usually a wire-format bug in the MCP."
)

# (slug, reason) for known Laserfiche errorCode values. Checked first because
# the server's own error code is more specific than HTTP status.
_ERROR_CODE_RULES: dict[int, tuple[str, str]] = {
    _LF_ERROR_CODE_AUTH_INVALID: ("auth_failed", _AUTH_REASON),
    _LF_ERROR_CODE_LFDS_UNREACHABLE: ("auth_failed", _AUTH_REASON),
    _LF_ERROR_CODE_REQUIRED_FIELD_A: ("required_field_missing", _REQUIRED_FIELD_REASON),
    _LF_ERROR_CODE_REQUIRED_FIELD_B: ("required_field_missing", _REQUIRED_FIELD_REASON),
}

# (slug, reason) for HTTP status codes. Checked after errorCode rules.
_HTTP_STATUS_RULES: dict[int, tuple[str, str]] = {
    401: ("auth_failed", _HTTP_AUTH_REASON),
    403: ("auth_failed", _HTTP_AUTH_REASON),
    404: ("not_found", "Server returned 404 — the entry, path, or endpoint does not exist."),
    405: ("method_not_allowed", _HTTP_405_REASON),
    415: ("unsupported_media_type", _HTTP_415_REASON),
    429: ("rate_limited", "HTTP 429 — slow down and retry after a delay."),
}


def _resolve_slug_and_reason(
    error_code: object,
    status: int | None,
    title: object,
    exc: LaserficheError,
) -> tuple[str, str]:
    """Pick the right (slug, reason) for an error.

    Order: Laserfiche errorCode → HTTP status code → generic 5xx → fallback.
    """
    if isinstance(error_code, int) and error_code in _ERROR_CODE_RULES:
        return _ERROR_CODE_RULES[error_code]
    if status is not None and status in _HTTP_STATUS_RULES:
        return _HTTP_STATUS_RULES[status]
    if status is not None and status >= 500:
        return "server_error", f"HTTP {status} from the Laserfiche server."
    return "server_error", str(title or str(exc)[:300])


def classify_lf_error(
    operation: str,
    exc: LaserficheError,
    *,
    entry_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a LaserficheError into a structured ``mode: error`` response.

    Output shape:
        {mode, operation, kind: <canonical 5>, error: <subkind>,
         status_code, server_error_code, server_message, reason,
         request_id, upstream_trace_id, [entry_id], ...extra}

    ``kind`` is one of the 5 canonical kinds (``not_found``,
    ``permission_denied``, ``rate_limited``, ``invalid_input``,
    ``upstream_unavailable``). ``error`` is the more-specific subkind.
    LLMs can branch on either granularity.

    ``upstream_trace_id`` is the Laserfiche server's W3C trace ID from
    the ProblemDetails response — operators use it to find the matching
    upstream log line. ``request_id`` is a UUID4 unique to this tool
    call, pivoting into the MCP's own logs.
    """
    detail = lf_error_detail(exc)
    error_code = detail.get("errorCode")
    title = detail.get("title") or detail.get("message")
    trace_id = detail.get("traceId")
    status = exc.status_code

    slug, reason = _resolve_slug_and_reason(error_code, status, title, exc)

    out: dict[str, Any] = {
        "mode": "error",
        "operation": operation,
        "kind": kind_for_subkind(slug),
        "error": slug,
        "status_code": status,
        "server_error_code": error_code,
        "server_message": title,
        "reason": reason,
        # request_id is the per-tool-call UUID set by tool_logger via
        # ContextVar. Outside a tool-logger context (direct test calls)
        # we fall back to a fresh UUID so the field is never null.
        "request_id": get_request_id_or_new(),
        "upstream_trace_id": trace_id,
    }
    if entry_id is not None:
        out["entry_id"] = entry_id
    if extra:
        out.update(extra)
    return out


def invalid_token_response(
    operation: str,
    entry_id: int,
    reason: str | None,
) -> dict[str, Any]:
    """Structured error for confirmation_token verification failures."""
    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "kind": "invalid_input",
        "error": "invalid_confirmation_token",
        "reason": reason,
        "request_id": get_request_id_or_new(),
        "next_step": (
            "Re-run the same tool without confirmation_token to get a fresh "
            "preview and a new token."
        ),
    }
