"""Template-assignment write tools."""

from __future__ import annotations

from typing import Any

from .. import _app
from ..errors import LaserficheError, classify_lf_error
from ._helpers import (
    ToolAbortedError,
    check_write_for_entry,
    require_writes_enabled,
    user_fields_to_values,
)
from ._registry import register
from ._validators import (
    validate_field_names,
    validate_required_fields,
    validate_template_name,
)


@register(v2_name="laserfiche_template_assign", is_write=True)
async def assign_template(
    entry_id: int,
    template_name: str,
    fields: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """Assign a template to an entry, optionally with initial field values.

    Use to attach a template (e.g. "Personnel Document", "Service Record")
    so the entry exposes that template's fields. Existing independent
    fields on the entry are unchanged. Fields common to the previously
    and newly assigned templates retain their values.

    Args:
        entry_id: Integer entry ID to template.
        template_name: Exact name of the template. Case-sensitive on most
            builds. Use ``list_template_definitions`` to discover what's
            available.
        fields: Optional initial field values to set in the same call.
            Mapping of field name → list of values (one item for
            single-value fields, many for multi-value). Often required
            because templates declare required fields — the validator
            below will tell you which.

    Returns: The server's updated entry on success, showing the new
    ``templateName`` and ``templateFieldNames``.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.
        - ``missing_required_fields`` — repository-wide required fields
          (``isRequired=true``, regardless of template membership) aren't
          set on the entry and weren't supplied in ``fields``. The
          response includes ``missing`` (names) and ``field_details``
          (with ``field_type``, ``list_values``, ``default_value``) so
          you can ask the user for valid values, or pick a default,
          then retry. Disable this check with
          ``LF_VALIDATE_REQUIRED_FIELDS=false``.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, "template_name": <str>, ...}``. Common slugs:
    ``required_field_missing`` (server-side equivalent if the validator
    is disabled), ``not_found`` (entry or template doesn't exist),
    ``auth_failed``.
    """
    require_writes_enabled()
    try:
        await check_write_for_entry("assign_template", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    template_err = await validate_template_name(
        "assign_template",
        template_name,
        entry_id=entry_id,
    )
    if template_err is not None:
        return template_err
    if fields:
        field_err = await validate_field_names(
            "assign_template",
            entry_id,
            list(fields.keys()),
        )
        if field_err is not None:
            return field_err
    validation_error = await validate_required_fields(
        "assign_template",
        entry_id,
        fields,
    )
    if validation_error is not None:
        return validation_error
    body_fields = user_fields_to_values(fields) if fields else None
    try:
        raw = await _app.get_client().assign_template(
            entry_id,
            template_name,
            fields=body_fields,
        )
    except LaserficheError as exc:
        return classify_lf_error(
            "assign_template",
            exc,
            entry_id=entry_id,
            extra={"template_name": template_name},
        )
    return raw


@register(v2_name="laserfiche_template_remove", is_write=True)
async def remove_template(entry_id: int) -> dict[str, Any]:
    """Clear the template assigned to an entry.

    Removes the template association; templated field values are
    cleared, independent fields are untouched. Use when the entry was
    misclassified or when changing categorization without picking a
    replacement template (otherwise just call ``assign_template`` —
    the server handles the swap atomically).

    Args:
        entry_id: Integer entry ID.

    Returns: The server's updated entry on success, with
    ``templateName=""`` and empty ``templateFieldNames``.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``,
    ``auth_failed``.
    """
    require_writes_enabled()
    try:
        await check_write_for_entry("remove_template", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    try:
        raw = await _app.get_client().remove_template(entry_id)
    except LaserficheError as exc:
        return classify_lf_error("remove_template", exc, entry_id=entry_id)
    return raw
