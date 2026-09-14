"""``delete_entry`` — the entry-removal write tool (requires confirmation token).

Folder deletes cascade through the entire subtree. The execute leg is async
on the server and returns an ``operation_token`` you can poll with
``wait_for_task``. See ``writes_delete_edoc_pages`` for the file-/page-level
deletes.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .. import _app, confirmation
from .._app import get_settings
from ..config import Settings
from ..errors import LaserficheError, classify_lf_error, invalid_token_response, local_error
from ._helpers import (
    ToolAbortedError,
    check_write_permission,
    entry_name,
    entry_path,
    entry_type,
    fetch_entry_for_op,
    require_writes_enabled,
)
from ._registry import register


async def _probe_immediate_child_count(
    entry_id: int,
    entry_kind: str,
    cap: int,
) -> tuple[int | None, bool, bool]:
    """Probe a folder's child count up to ``cap + 1``.

    Returns ``(child_count, exceeds_cap, probe_failed)``. On non-folders,
    returns ``(None, False, False)`` — the cap doesn't apply, and this
    isn't a failure. On HTTP error, returns ``(None, False, True)``: the
    safety cap exists to stop a huge/unbounded cascade delete, and an
    error means we genuinely don't know the child count — treating that
    as "0 children, safe to proceed" would defeat the cap's purpose, so
    the caller must fail closed rather than let the delete through.

    LFRepositoryAPI v1's OData ``$count`` is page-bound when combined with
    ``$top`` (returns page size, not total), so we count by fetching
    ``cap + 1`` children: if we got ``cap + 1`` back, the folder exceeds the
    cap (exact count unknown beyond it); otherwise the returned length IS
    the exact count.
    """
    if entry_kind != "Folder":
        return None, False, False
    try:
        listing = await _app.get_client().list_folder(entry_id, max_results=cap + 1)
    except LaserficheError:
        return None, False, True
    items = listing.get("value") or []
    if len(items) > cap:
        return None, True, False
    return len(items), False, False


def _delete_entry_preview(
    entry: dict[str, Any],
    entry_id: int,
    entry_kind: str,
    current_name: str,
    child_count: int | None,
    exceeds_cap: bool,
    probe_failed: bool,
    settings: Settings,
) -> dict[str, Any]:
    """Build the ``mode: preview`` response for ``delete_entry``."""
    token = confirmation.create_token("delete_entry", entry_id, current_name)

    if entry_kind == "Folder":
        descent = (
            f" and recursively delete its descendants ({child_count} immediate children observed)."
        )
    else:
        descent = "."
    if probe_failed:
        cap_warning = (
            " The immediate-child-count probe failed, so the batch cap "
            "could not be checked; execute will be refused "
            "(child_count_probe_failed) until the probe succeeds — retry "
            "this preview once the underlying error clears."
        )
    elif exceeds_cap:
        cap_warning = (
            f" Child count exceeds the configured cap "
            f"({settings.delete_folder_max_descendants}); execute "
            "will require force_large_delete=true."
        )
    else:
        cap_warning = ""

    return {
        "mode": "preview",
        "operation": "delete_entry",
        "entry_id": entry_id,
        "entry_name": current_name,
        "entry_type": entry_kind,
        "full_path": entry.get("fullPath") or entry.get("FullPath"),
        "immediate_child_count": child_count,
        "exceeds_batch_cap": exceeds_cap,
        "child_count_probe_failed": probe_failed,
        "batch_cap": settings.delete_folder_max_descendants,
        "audit_reason_required": settings.require_audit_reason,
        "warning": ("This will queue an irreversible delete of this entry" + descent + cap_warning),
        "confirmation_token": token,
        "ttl_seconds": confirmation.DEFAULT_TTL_SECONDS,
        "next_step": (
            "Surface this preview to the user. If they confirm, call "
            "delete_entry again with the same entry_id and the "
            "returned confirmation_token."
            + (" You will also need force_large_delete=true." if exceeds_cap else "")
            + (
                " audit_reason_id is required (LF_REQUIRE_AUDIT_REASON=true) "
                "— see get_audit_reasons."
                if settings.require_audit_reason
                else ""
            )
        ),
    }


def _delete_entry_check_caps(
    entry_id: int,
    child_count: int | None,
    exceeds_cap: bool,
    probe_failed: bool,
    force_large_delete: bool,
    audit_reason_id: int | None,
    settings: Settings,
) -> dict[str, Any] | None:
    """Returns an error response if execute-leg policy checks fail, else None."""
    if probe_failed:
        # The immediate-child-count probe (which the batch cap depends on)
        # errored out. Fail CLOSED: refuse the delete outright rather than
        # treating an unknown count as "0 children, safe" — that would
        # silently defeat LF_DELETE_FOLDER_MAX_DESCENDANTS on exactly the
        # transient-error case it exists to guard against, and unlike the
        # exceeds_batch_cap path, force_large_delete cannot bypass this:
        # the LLM has no real count to be confirming against.
        return local_error(
            "delete_entry",
            "child_count_probe_failed",
            entry_id=entry_id,
            reason=(
                "Could not determine this folder's immediate child count "
                "(the probe request failed), so the batch-delete safety "
                "cap (LF_DELETE_FOLDER_MAX_DESCENDANTS) cannot be "
                "verified. Refusing to proceed rather than assuming the "
                "folder is small. Retry once the underlying error clears."
            ),
        )

    if exceeds_cap and not force_large_delete:
        return local_error(
            "delete_entry",
            "exceeds_batch_cap",
            entry_id=entry_id,
            immediate_child_count=child_count,
            batch_cap=settings.delete_folder_max_descendants,
            reason=(
                f"Folder has {child_count} immediate children, which "
                f"exceeds the configured cap of "
                f"{settings.delete_folder_max_descendants} "
                "(LF_DELETE_FOLDER_MAX_DESCENDANTS). Pass "
                "force_large_delete=true on this call to proceed."
            ),
        )

    if settings.require_audit_reason and audit_reason_id is None:
        return local_error(
            "delete_entry",
            "audit_reason_required",
            entry_id=entry_id,
            reason=(
                "LF_REQUIRE_AUDIT_REASON=true; pass audit_reason_id (and "
                "optionally a comment). Use get_audit_reasons to enumerate "
                "valid IDs for the authenticated user."
            ),
        )
    return None


@register(v2_name="laserfiche_entry_delete", is_write=True)
async def delete_entry(
    entry_id: Annotated[
        int,
        Field(description="Integer entry ID to delete.", ge=1),
    ],
    confirmation_token: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "From the preview response. HMAC-signed, 5-minute TTL, "
                "bound to (operation, entry_id, entry_name). Omit to get "
                "a fresh preview; pass to execute."
            ),
        ),
    ] = None,
    audit_reason_id: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Required when LF_REQUIRE_AUDIT_REASON=true. Use "
                "get_audit_reasons to enumerate valid IDs for the "
                "authenticated user."
            ),
            ge=1,
        ),
    ] = None,
    comment: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional free-text comment recorded alongside the audit reason.",
            max_length=500,
        ),
    ] = None,
    *,
    force_large_delete: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Required when the folder's child count exceeds "
                "LF_DELETE_FOLDER_MAX_DESCENDANTS (default 50). The LLM "
                "has to explicitly opt in to a large delete."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Delete an entry. **Two-step: preview, then execute with token.**

    Irreversible; folders cascade to their entire subtree.

    Step 1: call without ``confirmation_token`` -> ``mode="preview"`` with
    ``entry_name``, ``full_path``, ``immediate_child_count``,
    ``exceeds_batch_cap``, a 5-minute ``confirmation_token``, and warnings.
    **Always surface the preview to the user before executing.**

    Step 2: re-call with the token. If the preview showed
    ``exceeds_batch_cap=true``, also pass ``force_large_delete=true``; if
    ``audit_reason_required=true``, supply ``audit_reason_id`` (from
    ``get_audit_reasons``). Execute returns ``{"mode": "executed",
    "operation_token"}`` — confirm completion via ``wait_for_task``.

    Pre-server errors: ``path_not_allowed``, ``invalid_confirmation_token``
    (expired/tampered — redo step 1), ``exceeds_batch_cap``,
    ``child_count_probe_failed`` (the batch-cap probe errored — refused,
    fail-closed, rather than assuming the folder is small; retry),
    ``audit_reason_required``. Server slugs: ``not_found``, ``auth_failed``.
    """
    require_writes_enabled()
    try:
        entry = await fetch_entry_for_op("delete_entry", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    perm_err = check_write_permission("delete_entry", path=entry_path(entry))
    if perm_err:
        return perm_err

    current_name = entry_name(entry)
    entry_kind = entry_type(entry)
    settings = get_settings()
    child_count, exceeds_cap, probe_failed = await _probe_immediate_child_count(
        entry_id,
        entry_kind,
        settings.delete_folder_max_descendants,
    )

    if confirmation_token is None:
        return _delete_entry_preview(
            entry,
            entry_id,
            entry_kind,
            current_name,
            child_count,
            exceeds_cap,
            probe_failed,
            settings,
        )

    ok, reason = confirmation.verify_token(
        confirmation_token,
        "delete_entry",
        entry_id,
        current_name,
    )
    if not ok:
        return invalid_token_response("delete_entry", entry_id, reason)

    cap_err = _delete_entry_check_caps(
        entry_id,
        child_count,
        exceeds_cap,
        probe_failed,
        force_large_delete,
        audit_reason_id,
        settings,
    )
    if cap_err is not None:
        return cap_err

    try:
        raw = await _app.get_client().delete_entry(
            entry_id,
            audit_reason_id=audit_reason_id,
            comment=comment,
        )
    except LaserficheError as exc:
        return classify_lf_error("delete_entry", exc, entry_id=entry_id)
    return {
        "mode": "executed",
        "operation": "delete_entry",
        "entry_id": entry_id,
        "entry_name": current_name,
        "operation_token": raw.get("token") or raw.get("Token"),
        "task_id": raw.get("taskId") or raw.get("TaskId"),
        "next_step": (
            "Async op queued. Call get_task_status(operation_token) or "
            "wait_for_task(operation_token) to confirm completion."
        ),
    }
