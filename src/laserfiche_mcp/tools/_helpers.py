"""Permission gates and entry-shape helpers shared by every write tool.

Translates ``LF_READ_ONLY`` / ``LF_WRITE_TOOLS_ALLOWED`` / ``LF_WRITE_PATHS_ALLOW`` /
``LF_WRITE_PATHS_DENY`` settings into structured ``mode: error`` responses
without ever hitting the network. Also owns the small accessors that pull
``name`` / ``entryType`` / ``fullPath`` out of an entry dict — both server
versions (v1 PascalCase, v2 camelCase) are handled.

All helpers return ``None`` on success or a ``{"mode": "error", ...}`` dict
the calling tool can return verbatim.
"""

from __future__ import annotations

from typing import Any

from .. import _app, permissions
from .._app import get_settings
from ..errors import LaserficheError, classify_lf_error


class ToolAbortedError(Exception):
    """A pre-API check failed; the tool short-circuits and returns ``payload``.

    Raised by ``fetch_entry_for_op``, ``check_write_for_entry``, and
    ``check_write_for_parent`` when either the entry fetch or a
    permission/allowlist check fails. ``payload`` is the structured
    ``{"mode": "error", ...}`` response that the calling tool returns
    to the LLM verbatim. Using an exception instead of a
    disjoint-tuple return lets call sites read straight-line:

        try:
            entry = await fetch_entry_for_op("op", entry_id)
        except ToolAbortedError as aborted:
            return aborted.payload

    rather than the older ``entry, fetch_err = await ...; if entry is
    None: assert fetch_err is not None; return fetch_err`` dance.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error") or payload.get("reason") or "tool aborted")
        self.payload = payload


def require_writes_enabled() -> None:
    """Defense-in-depth: write tools shouldn't be registered when read-only,
    but if anything slipped through, refuse to act."""
    if get_settings().read_only:
        raise RuntimeError(
            "Write operations are disabled (LF_READ_ONLY=true). Restart with "
            "LF_READ_ONLY=false to enable write tools."
        )


async def fetch_entry_for_op(
    operation: str,
    entry_id: int,
) -> dict[str, Any]:
    """Fetch an entry. Raises ``ToolAbortedError`` on HTTP error.

    Used by write tools that need the entry's metadata before acting
    (path-fence checks, preview-build, etc.). The raised exception
    carries the classified ``{"mode": "error", ...}`` payload so the
    caller can return it verbatim:

        try:
            entry = await fetch_entry_for_op("delete_entry", entry_id)
        except ToolAbortedError as aborted:
            return aborted.payload
    """
    try:
        return await _app.get_client().get_entry(entry_id)
    except LaserficheError as exc:
        raise ToolAbortedError(classify_lf_error(operation, exc, entry_id=entry_id)) from exc


def entry_name(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return ""
    return entry.get("name") or entry.get("Name") or ""


def entry_type(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return ""
    return entry.get("entryType") or entry.get("EntryType") or ""


def entry_path(entry: dict[str, Any] | None) -> str | None:
    if entry is None:
        return None
    return entry.get("fullPath") or entry.get("FullPath")


def check_write_permission(
    operation: str,
    *,
    path: str | None = None,
) -> dict[str, Any] | None:
    """Run pre-write guards. Returns None on success, or an error dict to return.

    Two checks, in order:
        1. Tool allowlist (``LF_WRITE_TOOLS_ALLOWED``) — refuses operations
           not in the operator-configured set. This is defense-in-depth on
           top of the registration-time filter, in case a tool is invoked
           directly (e.g., by the test suite).
        2. Path scope (``LF_WRITE_PATHS_ALLOW`` / ``LF_WRITE_PATHS_DENY``) —
           refuses mutations on entries outside the configured prefixes.

    The caller is responsible for fetching the entry (or its parent, for
    create ops) and passing its fullPath in. We don't fetch here because
    many tools already need the entry for other reasons (preview, token
    binding) and we want to avoid duplicate round-trips.
    """
    settings = get_settings()

    ok, reason = permissions.tool_allowed(operation, settings.write_tools_allowed)
    if not ok:
        return {
            "mode": "error",
            "operation": operation,
            "error": "tool_not_allowed",
            "reason": reason,
        }

    ok, reason = permissions.path_allowed(
        path,
        settings.write_paths_allow,
        settings.write_paths_deny,
    )
    if not ok:
        return {
            "mode": "error",
            "operation": operation,
            "error": "path_not_allowed",
            "reason": reason,
            "path": path,
        }

    return None


async def check_write_for_entry(operation: str, entry_id: int) -> dict[str, Any]:
    """Fetch entry and run write-permission checks. Returns the entry on success.

    Raises ``ToolAbortedError`` if either the fetch or the path-fence check
    fails; the exception's ``payload`` is the structured error response
    the calling tool should return verbatim.
    """
    entry = await fetch_entry_for_op(operation, entry_id)
    perm_err = check_write_permission(operation, path=entry_path(entry))
    if perm_err is not None:
        raise ToolAbortedError(perm_err)
    return entry


async def check_write_for_parent(operation: str, parent_id: int) -> dict[str, Any]:
    """Same as ``check_write_for_entry`` but checks the PARENT folder's path.

    Used by create operations (``create_folder``, ``copy_entry``,
    ``import_document``) where the target entry doesn't exist yet — we
    fence on where it would land.
    """
    parent = await fetch_entry_for_op(operation, parent_id)
    perm_err = check_write_permission(operation, path=entry_path(parent))
    if perm_err is not None:
        raise ToolAbortedError(perm_err)
    return parent


def fields_to_put_body(field_values: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert a ``get_field_values`` listing into the ``put_fields`` body shape.

    API returns: ``[{ fieldName, values: [{value, position}], ... }, ...]``.
    PUT expects:  ``{ FieldA: { values: [{value, position}] }, ... }``.
    """
    out: dict[str, Any] = {}
    for fv in field_values:
        name = fv.get("fieldName") or fv.get("FieldName")
        if not name:
            continue
        values = fv.get("values") or fv.get("Values") or []
        out[name] = {"values": values}
    return out


def user_fields_to_values(updates: dict[str, list[Any]]) -> dict[str, Any]:
    """Convert caller field updates into the API's ``FieldToUpdate`` shape.

    Example: ``{"Name": ["Smith"]}`` becomes
    ``{"Name": {"values": [{"value": "Smith", "position": 1}]}}``.

    Per the Repository API swagger, ``ValueToUpdate.position`` is
    1-indexed for multi-value fields and ignored for single-value
    fields, so we start at 1.
    """
    out: dict[str, Any] = {}
    for name, vals in updates.items():
        out[name] = {
            "values": [{"value": v, "position": i + 1} for i, v in enumerate(vals)],
        }
    return out
