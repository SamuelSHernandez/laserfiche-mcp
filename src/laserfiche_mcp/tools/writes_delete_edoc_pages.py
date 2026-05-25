"""Document-content destructive write tools: ``delete_edoc`` and ``delete_pages``.

Both require the standard preview + confirmation-token handshake. They
mutate the binary side of a document (the file or specific pages) but
leave the entry metadata, fields, template, links, and tags intact.

See ``writes_delete_entry`` for ``delete_entry``, which removes the
whole entry (and recursively all descendants for folders).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .. import _app, confirmation
from ..errors import LaserficheError, classify_lf_error, invalid_token_response
from ._helpers import (
    ToolAbortedError,
    check_write_permission,
    entry_name,
    entry_path,
    fetch_entry_for_op,
    require_writes_enabled,
)
from ._registry import register
from ._validators import validate_page_range_input


@register(v2_name="laserfiche_document_edoc_delete", is_write=True)
async def delete_edoc(
    entry_id: Annotated[
        int,
        Field(description="Integer entry ID of an electronic document.", ge=1),
    ],
    confirmation_token: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "From the preview response. HMAC-signed, 5-minute TTL. "
                "Omit to get a fresh preview; pass to execute."
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """Wipe a document's binary content while keeping the entry metadata.

    **Two-step: preview, then execute with token.** The entry, its
    template, its field values, its links and tags — all survive. Only
    the underlying file (the "edoc" — electronic document) is removed.
    Useful for retention scenarios where you must purge content but
    preserve the metadata audit trail.

    Irreversible without a Laserfiche backup. Use ``delete_entry`` if
    you actually want the entry gone.

    **Step 1 — preview**
        Call with ``confirmation_token=None``. Response includes the
        entry's full path, ``page_count``, ``extension``, and an
        HMAC-signed ``confirmation_token`` (5-minute TTL). Surface to
        the user.

    **Step 2 — execute**
        Re-call with the token. Returns ``{"mode": "executed", "result":
        ...}``.

    Args:
        entry_id: Integer entry ID of an electronic document.
        confirmation_token: From the preview. HMAC-signed, 5-minute TTL.

    Returns: On preview, ``{"mode": "preview", "confirmation_token": <str>,
    "full_path": <str>, "page_count": <int>, ...}``.
    On execute, ``{"mode": "executed", "entry_id": <int>, "entry_name":
    <str>, "result": <server response>}``.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.
        - ``invalid_confirmation_token`` — token expired or tampered.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found`` (entry is a
    folder, or has no edoc), ``method_not_allowed`` (server build doesn't
    expose this endpoint), ``auth_failed``.
    """
    require_writes_enabled()
    try:
        entry = await fetch_entry_for_op("delete_edoc", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    perm_err = check_write_permission("delete_edoc", path=entry_path(entry))
    if perm_err:
        return perm_err
    current_name = entry_name(entry)

    if confirmation_token is None:
        token = confirmation.create_token("delete_edoc", entry_id, current_name)
        return {
            "mode": "preview",
            "operation": "delete_edoc",
            "entry_id": entry_id,
            "entry_name": current_name,
            "full_path": entry.get("fullPath") or entry.get("FullPath"),
            "page_count": entry.get("pageCount") or entry.get("PageCount"),
            "extension": entry.get("extension") or entry.get("Extension"),
            "warning": (
                "This will permanently delete the document's binary content "
                "(edoc). The entry's metadata, fields, and template remain "
                "but the file itself is gone."
            ),
            "confirmation_token": token,
            "ttl_seconds": confirmation.DEFAULT_TTL_SECONDS,
            "next_step": (
                "Surface this preview to the user. If they confirm, call "
                "delete_edoc again with the same entry_id and the token."
            ),
        }

    ok, reason = confirmation.verify_token(
        confirmation_token,
        "delete_edoc",
        entry_id,
        current_name,
    )
    if not ok:
        return invalid_token_response("delete_edoc", entry_id, reason)

    try:
        raw = await _app.get_client().delete_edoc(entry_id)
    except LaserficheError as exc:
        return classify_lf_error("delete_edoc", exc, entry_id=entry_id)
    return {
        "mode": "executed",
        "operation": "delete_edoc",
        "entry_id": entry_id,
        "entry_name": current_name,
        "result": raw,
    }


@register(v2_name="laserfiche_document_pages_delete", is_write=True)
async def delete_pages(
    entry_id: Annotated[
        int,
        Field(description="Integer entry ID of a paginated document.", ge=1),
    ],
    page_range: Annotated[
        str,
        Field(
            description=(
                "Page-range expression. REQUIRED and non-empty. The API "
                "treats empty as 'delete all pages' — this tool refuses "
                "empty to remove that footgun. Pass an explicit wide "
                "range like '1-9999' if you genuinely want every page."
            ),
            examples=["1,2,3", "1-3,5", "2-7,10-12", "1-9999"],
            min_length=1,
        ),
    ],
    confirmation_token: Annotated[
        str | None,
        Field(
            default=None,
            description="From the preview. HMAC-signed, 5-minute TTL.",
        ),
    ] = None,
) -> dict[str, Any]:
    """Delete specific pages from a paginated document. **Two-step: preview, then execute.**

    Only valid on documents that the Laserfiche server treats as
    paginated (PDFs, TIFFs, scanned images). Plain-text files and Office
    documents have ``page_count: 0`` and will fail server-side with
    "Entry does not contain any pages" — use ``delete_edoc`` to wipe the
    whole file instead.

    Irreversible without a backup. Pages are renumbered after deletion,
    so a subsequent delete_pages("1-3") on the same document targets
    different physical pages than before.

    **Step 1 — preview**
        Call with ``confirmation_token=None``. Response includes
        ``page_count`` (server-reported, may be null on v1), the
        ``page_range`` you'll be deleting, and an HMAC-signed token.

    **Step 2 — execute**
        Re-call with the same ``page_range`` plus the token.

    Args:
        entry_id: Integer entry ID of a paginated document.
        page_range: Page-range expression. REQUIRED and non-empty.
            Examples: ``"1,2,3"`` (individual pages), ``"1-3,5"``
            (range + single), ``"2-7,10-12"`` (two ranges). The
            Laserfiche API treats empty as "delete all pages", so this
            tool refuses empty values to remove that footgun. If you
            genuinely want to delete every page, pass an explicit wide
            range like ``"1-9999"``.
        confirmation_token: From the preview. HMAC-signed, 5-minute TTL.

    Returns: On preview, ``{"mode": "preview", "confirmation_token": <str>,
    "page_count": <int>, "page_range": <str>, ...}``.
    On execute, ``{"mode": "executed", "entry_id": <int>, "page_range":
    <str>, "result": <server response>}``.

    Pre-server errors (returned before the API call):
        - ``page_range_required`` — ``page_range`` was empty/whitespace.
        - ``path_not_allowed`` — entry outside the allow list.
        - ``invalid_confirmation_token`` — token expired or tampered.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, "page_range": <str>, ...}``. Common slugs:
    ``not_found`` (entry doesn't exist or has no pages — common for
    non-paginated documents), ``method_not_allowed`` (server build
    doesn't expose this endpoint), ``auth_failed``.
    """
    require_writes_enabled()
    if not page_range or not page_range.strip():
        return {
            "mode": "error",
            "operation": "delete_pages",
            "error": "page_range_required",
            "message": (
                "page_range must be non-empty. The API would treat empty as "
                "'delete all pages' — too easy to fat-finger. Pass an "
                "explicit range like '1-9999' if you intended to delete all."
            ),
        }
    range_err = validate_page_range_input("delete_pages", entry_id, page_range)
    if range_err is not None:
        return range_err

    try:
        entry = await fetch_entry_for_op("delete_pages", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    perm_err = check_write_permission("delete_pages", path=entry_path(entry))
    if perm_err:
        return perm_err
    current_name = entry_name(entry)

    if confirmation_token is None:
        token = confirmation.create_token("delete_pages", entry_id, current_name)
        return {
            "mode": "preview",
            "operation": "delete_pages",
            "entry_id": entry_id,
            "entry_name": current_name,
            "full_path": entry.get("fullPath") or entry.get("FullPath"),
            "page_count": entry.get("pageCount") or entry.get("PageCount"),
            "page_range": page_range,
            "warning": (
                f"This will permanently delete pages matching {page_range!r} "
                "from the document. Page deletes are irreversible."
            ),
            "confirmation_token": token,
            "ttl_seconds": confirmation.DEFAULT_TTL_SECONDS,
            "next_step": (
                "Surface this preview to the user. If they confirm, call "
                "delete_pages again with the same entry_id, page_range, "
                "and the token."
            ),
        }

    ok, reason = confirmation.verify_token(
        confirmation_token,
        "delete_pages",
        entry_id,
        current_name,
    )
    if not ok:
        return invalid_token_response("delete_pages", entry_id, reason)

    try:
        raw = await _app.get_client().delete_pages(entry_id, page_range)
    except LaserficheError as exc:
        return classify_lf_error(
            "delete_pages",
            exc,
            entry_id=entry_id,
            extra={"page_range": page_range},
        )
    return {
        "mode": "executed",
        "operation": "delete_pages",
        "entry_id": entry_id,
        "entry_name": current_name,
        "page_range": page_range,
        "result": raw,
    }
