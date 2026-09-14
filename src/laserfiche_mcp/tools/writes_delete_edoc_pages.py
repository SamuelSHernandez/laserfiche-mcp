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
from ..errors import LaserficheError, classify_lf_error, invalid_token_response, local_error
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

    **Two-step: preview, then execute with token.** Entry, template,
    fields, links and tags all survive; only the file is removed
    (retention purges). Irreversible. Use ``delete_entry`` to remove the
    entry itself.

    Step 1: call without ``confirmation_token`` -> preview with
    ``full_path``, ``page_count``, ``extension`` and a 5-minute token —
    surface to the user. Step 2: re-call with the token.

    Pre-server errors: ``path_not_allowed``, ``invalid_confirmation_token``.
    Server slugs: ``not_found`` (folder or no edoc), ``method_not_allowed``,
    ``auth_failed``.
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

    Only for paginated documents (PDF/TIFF/scans); non-paginated entries
    fail server-side — use ``delete_edoc`` for those. Irreversible, and
    pages renumber after deletion.

    Step 1: call without ``confirmation_token`` -> preview with
    ``page_count``, the ``page_range``, and a 5-minute token. Step 2:
    re-call with the same ``page_range`` plus the token.

    ``page_range`` is required and non-empty (e.g. ``"1-3,5"``) — empty
    means "all pages" upstream, so it is refused; pass an explicit range.

    Pre-server errors: ``page_range_required``, ``invalid_page_range``,
    ``path_not_allowed``, ``invalid_confirmation_token``. Server slugs:
    ``not_found``, ``method_not_allowed``, ``auth_failed``.
    """
    require_writes_enabled()
    if not page_range or not page_range.strip():
        return local_error(
            "delete_pages",
            "page_range_required",
            message=(
                "page_range must be non-empty. The API would treat empty as "
                "'delete all pages' — too easy to fat-finger. Pass an "
                "explicit range like '1-9999' if you intended to delete all."
            ),
        )
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
        # Bind the page_range into the token: the user confirms deleting
        # THESE pages, so an execute call with a different range must fail.
        token = confirmation.create_token(
            "delete_pages", entry_id, current_name, params={"page_range": page_range}
        )
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
        params={"page_range": page_range},
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
