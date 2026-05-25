"""Write tools that create new entries: folders, copies, and document imports."""

from __future__ import annotations

import mimetypes
import os
from typing import Annotated, Any

from pydantic import Field

from .. import _app
from .._app import get_settings
from ..errors import LaserficheError, classify_lf_error
from ._helpers import (
    ToolAbortedError,
    check_write_for_parent,
    require_writes_enabled,
    user_fields_to_values,
)
from ._registry import register
from ._validators import (
    validate_field_names,
    validate_name,
    validate_tag_names,
    validate_template_name,
)


@register(v2_name="laserfiche_folder_create", is_write=True)
async def create_folder(
    parent_id: Annotated[
        int,
        Field(
            description=(
                "Integer entry ID of the destination folder. Root is "
                "typically ID 1. Resolve a path with get_entry_by_path "
                "first if you only have a path string."
            ),
            ge=1,
        ),
    ],
    name: Annotated[
        str,
        Field(
            description=(
                "New folder name. Backslashes, forward slashes, NUL bytes, "
                "and control characters are rejected. Max length 128."
            ),
            examples=["2024-Onboarding", "Q3 Reports"],
            min_length=1,
            max_length=128,
        ),
    ],
    template_name: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional template to assign on creation. Use "
                "list_template_definitions to discover names."
            ),
            examples=["Personnel Document"],
        ),
    ] = None,
    fields: Annotated[
        dict[str, list[Any]] | None,
        Field(
            default=None,
            description=(
                "Optional initial template-field values. Mapping of field "
                "name → list of values (one item per single-value field, "
                "many for multi-value). Required when the assigned "
                "template (or repo-wide required fields) demand them."
            ),
            examples=[{"Status": ["Active"], "Department": ["HR"]}],
        ),
    ] = None,
    *,
    auto_rename: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "When True, the server appends a numeric suffix if name "
                "already exists in the parent. When False (default), a "
                "collision returns an error."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Create a new folder as a child of ``parent_id``.

    Args:
        parent_id: Integer entry ID of the destination folder. Root is
            typically ID 1. Resolve a path to an ID with
            ``get_entry_by_path`` first if you only have a path string.
        name: New folder name. Backslashes are not allowed in entry
            names; pick something path-safe.
        template_name: Optional template to assign on creation. Use
            ``list_template_definitions`` to discover names.
        fields: Optional initial template-field values. Same shape as
            other field-writing tools: ``{"Status": ["Active"]}``. If
            the template (or repository-wide required fields) demand
            values you don't supply, the server will reject.
        auto_rename: When true, the server appends a numeric suffix if
            ``name`` already exists in the parent. When false (default),
            a collision returns an error.

    Returns: The server's entry payload for the new folder on success —
    ``id``, ``name``, ``parentId``, ``fullPath``, ``templateName``,
    ``creationTime``, ``creator``.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — parent's path falls outside
          ``LF_WRITE_PATHS_ALLOW`` / inside ``LF_WRITE_PATHS_DENY``.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "parent_id": <int>, "name": <str>, ...}``. Common slugs:
    ``not_found`` (parent doesn't exist), ``required_field_missing``
    (template you're assigning has required fields you didn't supply),
    ``auth_failed``.
    """
    require_writes_enabled()
    name_err = validate_name("create_folder", name, extra={"parent_id": parent_id})
    if name_err is not None:
        return name_err
    try:
        await check_write_for_parent("create_folder", parent_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    if template_name:
        template_err = await validate_template_name(
            "create_folder",
            template_name,
            extra={"parent_id": parent_id, "name": name},
        )
        if template_err is not None:
            return template_err
    if fields:
        field_err = await validate_field_names(
            "create_folder",
            parent_id,
            list(fields.keys()),
        )
        if field_err is not None:
            return field_err
    body_fields = user_fields_to_values(fields) if fields else None
    try:
        raw = await _app.get_client().create_child_entry(
            parent_id,
            entry_type="Folder",
            name=name,
            template_name=template_name,
            fields=body_fields,
            auto_rename=auto_rename,
        )
    except LaserficheError as exc:
        return classify_lf_error(
            "create_folder",
            exc,
            extra={"parent_id": parent_id, "name": name},
        )
    return raw


@register(v2_name="laserfiche_entry_copy", is_write=True)
async def copy_entry(
    source_id: Annotated[
        int,
        Field(description="Integer entry ID of the entry to copy.", ge=1),
    ],
    parent_id: Annotated[
        int,
        Field(description="Integer entry ID of the destination folder.", ge=1),
    ],
    name: Annotated[
        str,
        Field(
            description="New name for the copy. Must be path-safe (no backslashes).",
            min_length=1,
            max_length=128,
        ),
    ],
    *,
    auto_rename: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "When True, server appends a numeric suffix if name collides in the destination."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Copy an existing entry into a new location with a new name.

    Works for documents and folders (folders copy with their entire
    subtree — large copies can take minutes). The copy is server-side,
    so no bytes flow through the MCP. Original entry is unchanged.

    **Async**: returns immediately with an ``operation_token``. Poll
    completion with ``get_task_status`` or block with ``wait_for_task``
    — the final ``entryId`` for the new copy is in the task payload's
    ``entryId`` field once status is ``Completed``.

    Args:
        source_id: Integer entry ID of the entry to copy.
        parent_id: Integer entry ID of the destination folder.
        name: New name for the copy. Must be path-safe (no backslashes).
        auto_rename: When true, server appends a numeric suffix if
            ``name`` collides in the destination. Default false.

    Returns: ``{"token": <operation_token>}`` on success. Pass that
    token to ``wait_for_task(token)`` to get the actual copy result
    once it finishes.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — destination parent's path falls
          outside the allow list. (The source's path isn't fenced —
          this is a copy, not a move; the source is unchanged.)

    On failure: returns ``{"mode": "error", "error": <slug>,
    "source_id": <int>, "parent_id": <int>, "name": <str>, ...}``.
    Common slugs: ``not_found`` (source or parent doesn't exist),
    ``auth_failed``.
    """
    require_writes_enabled()
    name_err = validate_name(
        "copy_entry",
        name,
        extra={"source_id": source_id, "parent_id": parent_id},
    )
    if name_err is not None:
        return name_err
    try:
        await check_write_for_parent("copy_entry", parent_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    try:
        raw = await _app.get_client().copy_entry_async(
            parent_id,
            source_id=source_id,
            name=name,
            auto_rename=auto_rename,
        )
    except LaserficheError as exc:
        return classify_lf_error(
            "copy_entry",
            exc,
            extra={"source_id": source_id, "parent_id": parent_id, "name": name},
        )
    return raw


def _read_import_file(file_path: str, max_bytes: int) -> tuple[bytes, dict[str, Any] | None]:
    """Read the file, returning ``(bytes, None)`` or ``(b"", error_response)``."""
    if not os.path.isfile(file_path):
        return b"", {
            "mode": "error",
            "operation": "import_document",
            "error": "file_not_found",
            "file_path": file_path,
            "message": f"No file at {file_path!r}.",
        }

    size = os.path.getsize(file_path)
    if size > max_bytes:
        return b"", {
            "mode": "error",
            "operation": "import_document",
            "error": "size_exceeds_cap",
            "file_path": file_path,
            "byte_size": size,
            "max_bytes": max_bytes,
            "message": (
                f"File is {size} bytes, which exceeds the {max_bytes}-byte cap. "
                "Raise LF_IMPORT_MAX_BYTES if you really need this file."
            ),
        }

    with open(file_path, "rb") as fh:
        return fh.read(), None


def _build_import_metadata(
    template_name: str | None,
    fields: dict[str, list[Any]] | None,
    tags: list[str] | None,
) -> dict[str, Any] | None:
    """Assemble the multipart metadata payload, or None if no metadata."""
    if not (template_name or fields or tags):
        return None
    inner: dict[str, Any] = {}
    if template_name:
        inner["templateName"] = template_name
    if fields:
        inner["fields"] = user_fields_to_values(fields)
    if tags:
        inner["tags"] = tags
    return {"metadata": inner}


@register(v2_name="laserfiche_document_import", is_write=True)
async def import_document(
    parent_id: Annotated[
        int,
        Field(description="Integer entry ID of the destination folder.", ge=1),
    ],
    name: Annotated[
        str,
        Field(
            description=(
                "Filename to use inside Laserfiche (extension matters for "
                "content-type sniffing). Backslashes are not allowed."
            ),
            examples=["invoice-2024-Q3.pdf", "smith-john-resume.docx"],
            min_length=1,
            max_length=128,
        ),
    ],
    file_path: Annotated[
        str,
        Field(
            description=(
                "Absolute or working-directory-relative path to the local "
                "file. Must exist and be readable by the MCP process. "
                "Path is interpreted on the MCP server's filesystem — "
                "typically the same machine as Claude Desktop/Code."
            ),
            examples=["/tmp/uploads/invoice.pdf", "C:\\Users\\me\\Documents\\report.pdf"],
            min_length=1,
        ),
    ],
    template_name: Annotated[
        str | None,
        Field(default=None, description="Optional template to assign on import."),
    ] = None,
    fields: Annotated[
        dict[str, list[Any]] | None,
        Field(
            default=None,
            description="Optional template-field values to set on import.",
            examples=[{"Vendor": ["Acme"], "Invoice Date": ["2024-09-15"]}],
        ),
    ] = None,
    tags: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Optional list of tag names to attach. Tags must already "
                "exist as definitions (see list_tag_definitions)."
            ),
            examples=[["Confidential", "Q3-2024"]],
        ),
    ] = None,
    content_type: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Override the auto-detected MIME type. If omitted, the "
                "client sniffs from `name`'s extension."
            ),
            examples=[
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ],
        ),
    ] = None,
    *,
    auto_rename: Annotated[
        bool,
        Field(
            default=False,
            description="When True, server appends a numeric suffix if name collides.",
        ),
    ] = False,
) -> dict[str, Any]:
    """Upload a local file as a new document into a Laserfiche folder.

    The file is read from the local filesystem **as seen by the MCP
    server process** — typically the same machine that runs Claude
    Desktop/Code. The server reads, then POSTs the bytes as a multipart
    upload to the Laserfiche Repository API.

    Args:
        parent_id: Integer entry ID of the destination folder.
        name: Filename to use inside Laserfiche (extension matters for
            content-type sniffing). Backslashes are not allowed.
        file_path: Absolute or working-directory-relative path to the
            local file. Must exist and be readable by the MCP process.
        template_name: Optional template to assign on import.
        fields: Optional template-field values to set on import. Same
            shape as other field-writing tools.
        tags: Optional list of tag names to attach. Tags must already
            exist as definitions (see ``list_tag_definitions``).
        content_type: Override the auto-detected MIME type if needed
            (e.g. for unusual file extensions). The client sniffs from
            ``name`` if omitted.
        auto_rename: When true, server appends a numeric suffix if
            ``name`` collides in the destination.

    Returns: Server's import payload on success — ``operations``
    (with ``entryCreate.entryId`` for the new document) and
    ``documentLink`` (the API URL of the new entry).

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — parent's path outside the allow list.
        - ``file_not_found`` — ``file_path`` doesn't resolve to a real
          file on the MCP server's filesystem.
        - ``size_exceeds_cap`` — file is larger than ``LF_IMPORT_MAX_BYTES``
          (default 25 MB). The API caps at 100 MB; raise the env var to
          import files between those sizes.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "parent_id": <int>, "name": <str>, "file_path": <str>, ...}``.
    Common slugs: ``not_found`` (parent doesn't exist),
    ``required_field_missing`` (template demanded fields you didn't
    supply), ``auth_failed``.
    """
    require_writes_enabled()
    name_err = validate_name("import_document", name, extra={"parent_id": parent_id})
    if name_err is not None:
        return name_err
    try:
        await check_write_for_parent("import_document", parent_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    if template_name:
        template_err = await validate_template_name(
            "import_document",
            template_name,
            extra={"parent_id": parent_id, "name": name},
        )
        if template_err is not None:
            return template_err
    if fields:
        field_err = await validate_field_names(
            "import_document",
            parent_id,
            list(fields.keys()),
        )
        if field_err is not None:
            return field_err
    if tags:
        tag_err = await validate_tag_names("import_document", parent_id, tags)
        if tag_err is not None:
            return tag_err

    settings = get_settings()
    file_bytes, file_err = _read_import_file(file_path, settings.import_max_bytes)
    if file_err is not None:
        return file_err

    if content_type is None:
        guessed, _ = mimetypes.guess_type(name)
        content_type = guessed or "application/octet-stream"

    metadata = _build_import_metadata(template_name, fields, tags)

    try:
        raw = await _app.get_client().import_document(
            parent_id,
            name,
            file_bytes,
            content_type=content_type,
            metadata=metadata,
            auto_rename=auto_rename,
        )
    except LaserficheError as exc:
        return classify_lf_error(
            "import_document",
            exc,
            extra={"parent_id": parent_id, "name": name, "file_path": file_path},
        )
    return raw
