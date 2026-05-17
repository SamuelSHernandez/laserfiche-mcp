"""Defense-in-depth schema validators for write tools.

Runs cached schema lookups (field / template / tag / link definitions)
against the client and turns mismatches into structured ``mode: error``
responses with the valid alternatives listed. Without these the LLM
would get a raw HTTP 400 from the upstream server instead of a hint.

All validators return ``None`` on success or a ``{"mode": "error", ...}``
dict. Network failures during validation also return ``None`` — we'd
rather let the actual write surface the upstream error than block the
operation on a transient cache miss.
"""

from __future__ import annotations

from typing import Any

from .. import _app, permissions
from .._app import get_settings
from ..errors import LaserficheError


def validate_name(
    operation: str,
    name: str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a ``mode: error`` response if ``name`` fails entry-name validation."""
    ok, reason = permissions.name_allowed(name)
    if ok:
        return None
    out: dict[str, Any] = {
        "mode": "error",
        "operation": operation,
        "error": "invalid_name",
        "reason": reason,
        "name": name,
    }
    if extra:
        out.update(extra)
    return out


def validate_page_range_input(
    operation: str,
    entry_id: int,
    range_str: str,
) -> dict[str, Any] | None:
    """Return a ``mode: error`` response if ``range_str`` fails the syntax check.

    Catches malformed-but-non-empty inputs like ``"1, 2"``, ``"abc"``,
    ``"5-3"``, ``"0,1"``.
    """
    ok, reason = permissions.validate_page_range(range_str)
    if ok:
        return None
    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "invalid_page_range",
        "reason": reason,
        "page_range": range_str,
    }


async def validate_field_names(
    operation: str,
    entry_id: int,
    field_names: list[str],
) -> dict[str, Any] | None:
    """Return a ``mode: error`` response if any name is not a defined field.

    Uses the cached field-definition lookup on the client. Falls back to
    None on lookup failure (let the real PUT surface the upstream error).
    Skipped when ``LF_VALIDATE_NAMES=false``.
    """
    if not get_settings().validate_names:
        return None
    client = _app.get_client()
    try:
        defs = await client.cached_field_definitions()
    except Exception:  # noqa: BLE001 — validator is defense-in-depth; fall through
        return None
    unknown = [name for name in field_names if name not in defs]
    if not unknown:
        return None
    valid_sample = sorted(defs.keys())[:20]
    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "invalid_field_name",
        "reason": (
            f"Field name(s) {unknown!r} are not defined in this repository. "
            "Call list_field_definitions to see available fields."
        ),
        "invalid_field_names": unknown,
        "valid_field_names_sample": valid_sample,
    }


async def validate_template_name(
    operation: str,
    template_name: str,
    *,
    entry_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a ``mode: error`` response if ``template_name`` is not defined.

    Case-sensitive (matches the server's matching behavior on most v1
    builds). Falls back to None on lookup failure.
    """
    if not template_name:
        return None  # empty/None template_name is a clear-template signal
    if not get_settings().validate_names:
        return None
    client = _app.get_client()
    try:
        defs = await client.cached_template_definitions()
    except Exception:  # noqa: BLE001 — validator is defense-in-depth; fall through
        return None
    if template_name in defs:
        return None
    out: dict[str, Any] = {
        "mode": "error",
        "operation": operation,
        "error": "invalid_template_name",
        "reason": (
            f"Template {template_name!r} is not defined in this repository. "
            "Call list_template_definitions to see available templates. "
            "Match is case-sensitive."
        ),
        "template_name": template_name,
        "valid_template_names": sorted(defs.keys()),
    }
    if entry_id is not None:
        out["entry_id"] = entry_id
    if extra:
        out.update(extra)
    return out


async def validate_tag_names(
    operation: str,
    entry_id: int,
    tag_names: list[str],
) -> dict[str, Any] | None:
    """Return a ``mode: error`` response if any tag is not defined.

    Skips when the list is empty. Falls back to None on lookup failure.
    Skipped entirely when ``LF_VALIDATE_NAMES=false``.
    """
    if not tag_names:
        return None
    if not get_settings().validate_names:
        return None
    client = _app.get_client()
    try:
        defs = await client.cached_tag_definitions()
    except Exception:  # noqa: BLE001 — validator is defense-in-depth; fall through
        return None
    unknown = [name for name in tag_names if name not in defs]
    if not unknown:
        return None
    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "invalid_tag_name",
        "reason": (
            f"Tag name(s) {unknown!r} are not defined in this repository. "
            "Call list_tag_definitions to see available tags."
        ),
        "invalid_tag_names": unknown,
        "valid_tag_names": sorted(defs.keys()),
    }


async def validate_link_types(
    operation: str,
    entry_id: int,
    link_type_ids: list[int],
) -> dict[str, Any] | None:
    """Return a ``mode: error`` response if any linkTypeId is undefined.

    Falls back to None on lookup failure. Skipped when ``LF_VALIDATE_NAMES=false``.
    """
    if not link_type_ids:
        return None
    if not get_settings().validate_names:
        return None
    client = _app.get_client()
    try:
        defs = await client.cached_link_definitions()
    except Exception:  # noqa: BLE001 — validator is defense-in-depth; fall through
        return None
    unknown = [lid for lid in link_type_ids if lid not in defs]
    if not unknown:
        return None
    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "invalid_link_type",
        "reason": (
            f"linkTypeId(s) {unknown!r} are not defined in this repository. "
            "Call list_link_definitions to see available link types."
        ),
        "invalid_link_type_ids": unknown,
        "valid_link_type_ids": sorted(defs.keys()),
    }


async def validate_required_fields(
    operation: str,
    entry_id: int,
    caller_fields: dict[str, list[Any]] | None,
) -> dict[str, Any] | None:
    """Return a ``mode: error`` response if repo-required fields are missing.

    Returns None when everything required is either already on the entry
    or being supplied in ``caller_fields``. Skipped when
    ``LF_VALIDATE_REQUIRED_FIELDS`` is false. Falls back to None on any
    validation-read failure so the real PUT still runs and the server's
    own error path is what surfaces.
    """
    settings = get_settings()
    if not settings.validate_required_fields:
        return None
    client = _app.get_client()
    try:
        defs = await client.list_field_definitions(max_results=500, skip=0)
        current = await client.get_field_values(entry_id)
    except LaserficheError:
        return None  # let the actual call surface the error

    required_names: list[dict[str, Any]] = []
    for fd in defs.get("value") or []:
        if fd.get("isRequired"):
            required_names.append(fd)
    if not required_names:
        return None

    already_set = {
        (fv.get("fieldName") or "") for fv in (current.get("value") or []) if fv.get("values")
    }
    being_supplied = set(caller_fields.keys()) if caller_fields else set()

    missing = [
        fd
        for fd in required_names
        if fd["name"] not in already_set and fd["name"] not in being_supplied
    ]
    if not missing:
        return None

    return {
        "mode": "error",
        "operation": operation,
        "entry_id": entry_id,
        "error": "missing_required_fields",
        "missing": [fd["name"] for fd in missing],
        "field_details": [
            {
                "name": fd["name"],
                "field_type": fd.get("fieldType"),
                "list_values": fd.get("listValues") or [],
                "default_value": fd.get("defaultValue"),
            }
            for fd in missing
        ],
        "next_step": (
            f"Call {operation} again with `fields=` including each of "
            f"these names. List fields offer constraint values via "
            f"list_values; date/text fields accept literal values. "
            f"Disable this check with LF_VALIDATE_REQUIRED_FIELDS=false."
        ),
    }
