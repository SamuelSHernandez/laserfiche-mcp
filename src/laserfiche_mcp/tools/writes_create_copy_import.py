"""Write tools that create new entries: folders, copies, and document imports."""

from __future__ import annotations

import mimetypes
import os
from typing import Annotated, Any

from pydantic import Field

from .. import _app, permissions
from .._app import get_settings
from ..errors import LaserficheError, classify_lf_error, local_error
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

    Optional ``template_name``/``fields`` apply metadata on creation;
    ``auto_rename`` resolves name collisions with a numeric suffix.

    Returns the new folder's entry payload. Pre-server error:
    ``path_not_allowed``. Server slugs: ``not_found``,
    ``required_field_missing``, ``auth_failed``.
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

    Server-side copy of documents or folders (subtree included; large
    copies take minutes). **Async**: returns ``{"token"}`` immediately —
    pass it to ``wait_for_task`` for the new ``entryId``. The source is
    unchanged and not path-fenced; the destination is.

    Pre-server error: ``path_not_allowed`` (destination). Server slugs:
    ``not_found``, ``auth_failed``.
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
        return b"", local_error(
            "import_document",
            "file_not_found",
            file_path=file_path,
            message=f"No file at {file_path!r}.",
        )

    size = os.path.getsize(file_path)
    if size > max_bytes:
        return b"", local_error(
            "import_document",
            "size_exceeds_cap",
            file_path=file_path,
            byte_size=size,
            max_bytes=max_bytes,
            message=(
                f"File is {size} bytes, which exceeds the {max_bytes}-byte cap. "
                "Raise LF_IMPORT_MAX_BYTES if you really need this file."
            ),
        )

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
                "typically the same machine as Claude Desktop/Code. When "
                "LF_IMPORT_SOURCE_DIRS is configured, the path must resolve "
                "inside one of those directories."
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

    ``file_path`` is read from the MCP server process's filesystem, then
    POSTed as multipart. Optional ``template_name``/``fields``/``tags``
    apply metadata on import.

    Returns the server's import payload (``entryCreate.entryId`` = new
    document). Pre-server errors: ``path_not_allowed`` (destination),
    ``source_path_not_allowed`` (LF_IMPORT_SOURCE_DIRS), ``file_not_found``,
    ``size_exceeds_cap`` (LF_IMPORT_MAX_BYTES, default 25 MB; API caps at
    100 MB). Server slugs: ``not_found``, ``required_field_missing``,
    ``auth_failed``.
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
    source_ok, source_reason = permissions.local_source_path_allowed(
        file_path, settings.import_source_dirs
    )
    if not source_ok:
        return local_error(
            "import_document",
            "source_path_not_allowed",
            file_path=file_path,
            reason=source_reason,
        )

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
