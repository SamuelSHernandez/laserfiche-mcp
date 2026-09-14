"""Template-assignment write tools."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

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
    entry_id: Annotated[
        int,
        Field(description="Integer entry ID to template.", ge=1),
    ],
    template_name: Annotated[
        str,
        Field(
            description=(
                "Exact name of the template. Case-sensitive on most builds. "
                "Use list_template_definitions or get_template_fields to "
                "discover what's available."
            ),
            examples=["Personnel Document", "Invoice", "Loan Application"],
        ),
    ],
    fields: Annotated[
        dict[str, list[Any]] | None,
        Field(
            default=None,
            description=(
                "Optional initial field values to set in the same call. "
                "Mapping of field name → list of values (one item for "
                "single-value, many for multi-value). Often required when "
                "the template declares required fields — the validator "
                "tells you which."
            ),
            examples=[{"Last Name": ["Smith"], "Hire Date": ["2024-01-15"]}],
        ),
    ] = None,
) -> dict[str, Any]:
    """Assign a template to an entry, optionally with initial field values.

    Existing independent fields are unchanged; fields shared with the
    previous template keep their values. Templates often declare required
    fields — use ``get_template_fields`` first to build ``fields``.

    Returns the updated entry. Pre-server errors: ``path_not_allowed``;
    ``missing_required_fields`` lists ``missing`` and ``field_details`` so
    you can ask the user for values and retry (disable the check with
    LF_VALIDATE_REQUIRED_FIELDS=false). Server slugs:
    ``required_field_missing``, ``not_found``, ``auth_failed``.
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
        template_name=template_name,
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
async def remove_template(
    entry_id: Annotated[int, Field(description="Integer entry ID.", ge=1)],
) -> dict[str, Any]:
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
