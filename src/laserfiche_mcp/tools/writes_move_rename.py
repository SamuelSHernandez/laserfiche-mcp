"""Two-step (preview + confirmation token) rename and move tools."""

from __future__ import annotations

from typing import Any

from .. import _app, confirmation
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
from ._validators import validate_name


def _rename_preview(
    entry: dict[str, Any],
    entry_id: int,
    new_name: str,
    current_name: str,
) -> dict[str, Any]:
    """Build the ``mode: preview`` response for ``rename_entry``."""
    token = confirmation.create_token("rename_entry", entry_id, current_name)
    current_path = entry.get("fullPath") or entry.get("FullPath") or ""
    folder_path = entry.get("folderPath") or entry.get("FolderPath")
    if folder_path:
        would_be_path = f"{folder_path}\\{new_name}".rstrip("\\")
    elif current_path:
        sep = current_path.rfind("\\")
        would_be_path = current_path[: sep + 1] + new_name if sep >= 0 else new_name
    else:
        would_be_path = new_name
    return {
        "mode": "preview",
        "operation": "rename_entry",
        "entry_id": entry_id,
        "current_name": current_name,
        "new_name": new_name,
        "current_full_path": current_path,
        "would_be_full_path": would_be_path,
        "entry_type": entry_type(entry),
        "warning": (
            "Renaming changes the entry's full path, which can break "
            "external references (links, shortcuts, bookmarks)."
        ),
        "confirmation_token": token,
        "ttl_seconds": confirmation.DEFAULT_TTL_SECONDS,
        "next_step": (
            "Surface this preview to the user. If they confirm, call "
            "rename_entry again with the same entry_id and new_name, "
            "passing the confirmation_token argument."
        ),
    }


@register(v2_name="laserfiche_entry_rename", is_write=True)
async def rename_entry(
    entry_id: int,
    new_name: str,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Rename an entry. **Two-step: preview first, then execute with token.**

    Renaming changes the entry's name AND its ``fullPath``. External
    references (shortcuts, links from other entries, deep links into the
    web client, hardcoded paths in workflows) break — read the preview's
    ``would_be_full_path`` carefully and confirm with the user before
    passing the token back.

    **Step 1 — preview**
        Call with ``confirmation_token=None`` (omitted). Response:

        ```json
        {
          "mode": "preview",
          "operation": "rename_entry",
          "entry_id": 84493,
          "current_name": "probe.txt",
          "new_name": "probe-final.txt",
          "current_full_path": "\\Sandbox\\probe.txt",
          "would_be_full_path": "\\Sandbox\\probe-final.txt",
          "entry_type": "Document",
          "warning": "Renaming changes the entry's full path ...",
          "confirmation_token": "<opaque base64>",
          "ttl_seconds": 300,
          "next_step": "Surface this preview ..."
        }
        ```

        Surface the preview to the user. Do NOT silently round-trip both
        calls without showing the user the would-be path.

    **Step 2 — execute**
        Re-call with the same ``entry_id``, the same ``new_name``, and
        the ``confirmation_token`` from step 1. Returns
        ``{"mode": "executed", ..., "result": <updated entry>}``.

    Args:
        entry_id: Integer entry ID.
        new_name: New name. Path-safe (no backslashes).
        confirmation_token: From the preview response. HMAC-signed and
            bound to ``(operation, entry_id, current entry name)`` — a
            token can't be replayed for a different entry. Expires after
            5 minutes; server restart invalidates all pending tokens.

    Returns: On preview, ``{"mode": "preview", "confirmation_token": <str>,
    "current_full_path": ..., "would_be_full_path": ..., ...}``.
    On execute, ``{"mode": "executed", "entry_id": <int>, "old_name":
    <str>, "new_name": <str>, "result": <updated entry>}``.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry's path outside the allow list.
        - ``invalid_confirmation_token`` — token expired, tampered, or
          for a different entry. Re-run step 1 to get a fresh one.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, "new_name": <str>, ...}``. Common slugs:
    ``not_found``, ``auth_failed``.
    """
    require_writes_enabled()
    name_err = validate_name("rename_entry", new_name, extra={"entry_id": entry_id})
    if name_err is not None:
        return name_err
    try:
        entry = await fetch_entry_for_op("rename_entry", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    perm_err = check_write_permission("rename_entry", path=entry_path(entry))
    if perm_err:
        return perm_err
    current_name = entry_name(entry)

    if confirmation_token is None:
        return _rename_preview(entry, entry_id, new_name, current_name)

    ok, reason = confirmation.verify_token(
        confirmation_token,
        "rename_entry",
        entry_id,
        current_name,
    )
    if not ok:
        return invalid_token_response("rename_entry", entry_id, reason)

    try:
        raw = await _app.get_client().patch_entry(entry_id, name=new_name)
    except LaserficheError as exc:
        return classify_lf_error(
            "rename_entry",
            exc,
            entry_id=entry_id,
            extra={"new_name": new_name},
        )
    return {
        "mode": "executed",
        "operation": "rename_entry",
        "entry_id": entry_id,
        "old_name": current_name,
        "new_name": new_name,
        "result": raw,
    }


def _move_preview(
    entry: dict[str, Any],
    entry_id: int,
    new_parent_id: int,
    new_name: str | None,
    current_name: str,
    target_path: str,
) -> dict[str, Any]:
    """Build the ``mode: preview`` response for ``move_entry``."""
    token = confirmation.create_token("move_entry", entry_id, current_name)
    final_name = new_name or current_name
    would_be_path = f"{target_path}\\{final_name}".rstrip("\\") if target_path else final_name
    return {
        "mode": "preview",
        "operation": "move_entry",
        "entry_id": entry_id,
        "current_name": current_name,
        "new_name": new_name,
        "new_parent_id": new_parent_id,
        "current_full_path": entry.get("fullPath") or entry.get("FullPath"),
        "would_be_full_path": would_be_path,
        "entry_type": entry_type(entry),
        "warning": (
            "Moving an entry changes its full path. Folder moves take "
            "the entire subtree along. External references (links, "
            "shortcuts, bookmarks) by path will break."
        ),
        "confirmation_token": token,
        "ttl_seconds": confirmation.DEFAULT_TTL_SECONDS,
        "next_step": (
            "Surface this preview to the user. If they confirm, call "
            "move_entry again with the same arguments, passing the "
            "confirmation_token."
        ),
    }


@register(v2_name="laserfiche_entry_move", is_write=True)
async def move_entry(
    entry_id: int,
    new_parent_id: int,
    confirmation_token: str | None = None,
    new_name: str | None = None,
) -> dict[str, Any]:
    """Move an entry to a different parent folder. **Two-step: preview, then execute.**

    Same path-change risks as ``rename_entry`` — external references
    break when the entry's ``fullPath`` changes. Folder moves carry the
    whole subtree along, so the blast radius is the whole subtree's
    descendant paths.

    **Step 1 — preview**
        Call with ``confirmation_token=None``. Response includes
        ``current_full_path`` and ``would_be_full_path`` (computed from
        the destination parent), plus an HMAC-signed
        ``confirmation_token`` valid for 5 minutes. Surface to the user.

    **Step 2 — execute**
        Re-call with the same arguments plus the token.

    **Path-fence note**: the path check runs on BOTH the source AND the
    destination. A token issued for an allowed source path cannot be
    replayed to land in a denied destination — the destination's path is
    refetched on the execute leg and checked again.

    Args:
        entry_id: Integer entry ID of the entry to move.
        new_parent_id: Integer entry ID of the destination folder.
        new_name: Optional rename to apply in the same operation. If
            omitted, the entry keeps its current name in the new
            location. Path-safe (no backslashes).
        confirmation_token: From the preview response. HMAC-signed,
            5-minute TTL, server-restart-invalidating.

    Returns: On preview, ``{"mode": "preview", "confirmation_token": <str>,
    "current_full_path": ..., "would_be_full_path": ..., ...}``.
    On execute, ``{"mode": "executed", "entry_id": <int>, "old_name":
    <str>, "new_parent_id": <int>, "new_name": <str>, "result": <updated entry>}``.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — source OR destination falls outside
          the allow list.
        - ``invalid_confirmation_token`` — token expired or tampered.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, "new_parent_id": <int>, ...}``. Common slugs:
    ``not_found`` (entry or destination doesn't exist), ``auth_failed``.
    """
    require_writes_enabled()
    if new_name is not None:
        name_err = validate_name(
            "move_entry",
            new_name,
            extra={"entry_id": entry_id, "new_parent_id": new_parent_id},
        )
        if name_err is not None:
            return name_err
    try:
        entry = await fetch_entry_for_op("move_entry", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    src_err = check_write_permission("move_entry", path=entry_path(entry))
    if src_err:
        return src_err
    current_name = entry_name(entry)

    # Also fence on the destination — moving an allowed-path entry into
    # a denied folder is still a write-into-deny-zone. The destination's
    # path is refetched on the execute leg so a token issued for one
    # destination can't be replayed to land in a different one.
    try:
        target = await _app.get_client().get_entry(new_parent_id)
        target_path = target.get("fullPath") or target.get("FullPath") or ""
    except LaserficheError:
        target_path = ""
    if target_path:
        dest_err = check_write_permission("move_entry", path=target_path)
        if dest_err:
            return dest_err

    if confirmation_token is None:
        return _move_preview(entry, entry_id, new_parent_id, new_name, current_name, target_path)

    ok, reason = confirmation.verify_token(
        confirmation_token,
        "move_entry",
        entry_id,
        current_name,
    )
    if not ok:
        return invalid_token_response("move_entry", entry_id, reason)

    try:
        raw = await _app.get_client().patch_entry(
            entry_id,
            parent_id=new_parent_id,
            name=new_name,
        )
    except LaserficheError as exc:
        return classify_lf_error(
            "move_entry",
            exc,
            entry_id=entry_id,
            extra={"new_parent_id": new_parent_id},
        )
    return {
        "mode": "executed",
        "operation": "move_entry",
        "entry_id": entry_id,
        "old_name": current_name,
        "new_parent_id": new_parent_id,
        "new_name": new_name,
        "result": raw,
    }
