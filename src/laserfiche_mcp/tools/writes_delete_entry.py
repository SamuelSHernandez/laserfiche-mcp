"""``delete_entry`` — the entry-removal write tool (requires confirmation token).

Folder deletes cascade through the entire subtree. The execute leg is async
on the server and returns an ``operation_token`` you can poll with
``wait_for_task``. See ``writes_delete_edoc_pages`` for the file-/page-level
deletes.
"""

from __future__ import annotations

from typing import Any

from .. import _app, confirmation
from .._app import get_settings
from ..config import Settings
from ..errors import LaserficheError, classify_lf_error, invalid_token_response
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
) -> tuple[int | None, bool]:
    """Probe a folder's child count up to ``cap + 1``.

    Returns ``(child_count, exceeds_cap)``. On non-folders, returns
    ``(None, False)``. On HTTP error, returns ``(None, False)`` — let the
    actual delete surface the failure.

    LFRepositoryAPI v1's OData ``$count`` is page-bound when combined with
    ``$top`` (returns page size, not total), so we count by fetching
    ``cap + 1`` children: if we got ``cap + 1`` back, the folder exceeds the
    cap (exact count unknown beyond it); otherwise the returned length IS
    the exact count.
    """
    if entry_kind != "Folder":
        return None, False
    try:
        listing = await _app.get_client().list_folder(entry_id, max_results=cap + 1)
    except LaserficheError:
        return None, False
    items = listing.get("value") or []
    if len(items) > cap:
        return None, True
    return len(items), False


def _delete_entry_preview(
    entry: dict[str, Any],
    entry_id: int,
    entry_kind: str,
    current_name: str,
    child_count: int | None,
    exceeds_cap: bool,
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
    cap_warning = (
        f" Child count exceeds the configured cap "
        f"({settings.delete_folder_max_descendants}); execute "
        "will require force_large_delete=true."
        if exceeds_cap
        else ""
    )

    return {
        "mode": "preview",
        "operation": "delete_entry",
        "entry_id": entry_id,
        "entry_name": current_name,
        "entry_type": entry_kind,
        "full_path": entry.get("fullPath") or entry.get("FullPath"),
        "immediate_child_count": child_count,
        "exceeds_batch_cap": exceeds_cap,
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
    force_large_delete: bool,
    audit_reason_id: int | None,
    settings: Settings,
) -> dict[str, Any] | None:
    """Returns an error response if execute-leg policy checks fail, else None."""
    if exceeds_cap and not force_large_delete:
        return {
            "mode": "error",
            "operation": "delete_entry",
            "entry_id": entry_id,
            "error": "exceeds_batch_cap",
            "immediate_child_count": child_count,
            "batch_cap": settings.delete_folder_max_descendants,
            "reason": (
                f"Folder has {child_count} immediate children, which "
                f"exceeds the configured cap of "
                f"{settings.delete_folder_max_descendants} "
                "(LF_DELETE_FOLDER_MAX_DESCENDANTS). Pass "
                "force_large_delete=true on this call to proceed."
            ),
        }

    if settings.require_audit_reason and audit_reason_id is None:
        return {
            "mode": "error",
            "operation": "delete_entry",
            "entry_id": entry_id,
            "error": "audit_reason_required",
            "reason": (
                "LF_REQUIRE_AUDIT_REASON=true; pass audit_reason_id (and "
                "optionally a comment). Use get_audit_reasons to enumerate "
                "valid IDs for the authenticated user."
            ),
        }
    return None


@register(v2_name="laserfiche_entry_delete", is_write=True)
async def delete_entry(
    entry_id: int,
    confirmation_token: str | None = None,
    audit_reason_id: int | None = None,
    comment: str | None = None,
    *,
    force_large_delete: bool = False,
) -> dict[str, Any]:
    """Delete an entry. **Two-step: preview, then execute with token.**

    Irreversible without a Laserfiche backup or recycle bin restore.
    Folders cascade — the entire subtree (every document, every
    subfolder) goes with them. The delete itself is async on the server,
    so the execute call returns an ``operation_token`` you can pass to
    ``wait_for_task`` for confirmation.

    **Step 1 — preview**
        Call with ``confirmation_token=None``. Response:

        ```json
        {
          "mode": "preview",
          "operation": "delete_entry",
          "entry_id": 84490,
          "entry_name": "probe-2",
          "entry_type": "Folder",
          "full_path": "\\Sandbox\\probe-2",
          "immediate_child_count": 2,
          "exceeds_batch_cap": false,
          "batch_cap": 10,
          "audit_reason_required": false,
          "warning": "...recursively delete its descendants (2 immediate children observed).",
          "confirmation_token": "<opaque>",
          "ttl_seconds": 300,
          "next_step": "..."
        }
        ```

        Always surface to the user before passing the token back. The
        ``immediate_child_count`` is an exact count when ≤ batch_cap; if
        ``exceeds_batch_cap=true``, the exact count is unknown but
        ``immediate_child_count`` will be null and the folder has at
        least ``batch_cap + 1`` children.

    **Step 2 — execute**
        Re-call with the ``confirmation_token``. If the preview showed
        ``exceeds_batch_cap=true``, also pass ``force_large_delete=true``
        on the execute leg. If ``audit_reason_required=true``, supply
        ``audit_reason_id`` from ``get_audit_reasons``.

    Args:
        entry_id: Integer entry ID to delete.
        confirmation_token: From the preview response. HMAC-signed,
            5-minute TTL.
        audit_reason_id: Required when ``LF_REQUIRE_AUDIT_REASON=true``.
            Use ``get_audit_reasons`` to enumerate valid IDs.
        comment: Optional free-text comment recorded alongside the
            audit reason.
        force_large_delete: Required when the folder's child count
            exceeds ``LF_DELETE_FOLDER_MAX_DESCENDANTS`` (default 50);
            independent of the confirmation token, so the LLM has to
            explicitly opt in to a large delete.

    Returns on execute: ``{"mode": "executed", "operation_token":
    "<uuid>", ...}``. Pass ``operation_token`` to ``wait_for_task`` to
    confirm completion.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.
        - ``invalid_confirmation_token`` — token expired or tampered.
        - ``exceeds_batch_cap`` — folder exceeds the cap and
          ``force_large_delete`` wasn't passed. Response includes
          ``immediate_child_count`` (when known) and ``batch_cap``.
        - ``audit_reason_required`` — env demands an audit reason but
          ``audit_reason_id`` wasn't supplied.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``,
    ``auth_failed``.
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
    child_count, exceeds_cap = await _probe_immediate_child_count(
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
        entry_id, child_count, exceeds_cap, force_large_delete, audit_reason_id, settings
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
