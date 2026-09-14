"""Two-step (preview + confirmation token) rename and move tools."""

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
    # Bind new_name: the user confirms renaming to THIS name, so an
    # execute call with a different new_name must fail token verification.
    token = confirmation.create_token(
        "rename_entry", entry_id, current_name, params={"new_name": new_name}
    )
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
    entry_id: Annotated[
        int,
        Field(description="Integer entry ID to rename.", ge=1),
    ],
    new_name: Annotated[
        str,
        Field(
            description=(
                "New name to apply. Path-safe (no backslashes, forward "
                "slashes, NUL bytes, or control characters). Max 128 chars."
            ),
            examples=["report-final.pdf", "Smith,John-renamed.pdf"],
            min_length=1,
            max_length=128,
        ),
    ],
    confirmation_token: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "From the preview response. HMAC-signed, 5-minute TTL, "
                "bound to (operation, entry_id, current_name, new_name). "
                "Omit to get a fresh preview; pass to execute with the SAME "
                "new_name that was previewed."
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """Rename an entry. **Two-step: preview first, then execute with token.**

    Renaming changes the entry's ``fullPath``; external references break.

    Step 1: call without ``confirmation_token`` -> ``mode="preview"`` with
    ``current_full_path``, ``would_be_full_path`` and a 5-minute token
    bound to (operation, entry_id, current name). **Surface the would-be
    path to the user — never silently round-trip both calls.**

    Step 2: re-call with the same args plus the token -> ``mode="executed"``
    with the updated entry.

    Pre-server errors: ``path_not_allowed``, ``invalid_confirmation_token``
    (expired/tampered/wrong entry — redo step 1). Server slugs:
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
        params={"new_name": new_name},
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
    # Bind the destination (and optional rename): the user confirms moving
    # to THIS folder under THIS name, so an execute call with a different
    # new_parent_id or new_name must fail token verification.
    token = confirmation.create_token(
        "move_entry",
        entry_id,
        current_name,
        params={"new_parent_id": new_parent_id, "new_name": new_name},
    )
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
    entry_id: Annotated[
        int,
        Field(description="Integer entry ID of the entry to move.", ge=1),
    ],
    new_parent_id: Annotated[
        int,
        Field(description="Integer entry ID of the destination folder.", ge=1),
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
    new_name: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional rename to apply in the same operation. If "
                "omitted, the entry keeps its current name in the new "
                "location. Path-safe (no backslashes)."
            ),
            min_length=1,
            max_length=128,
        ),
    ] = None,
) -> dict[str, Any]:
    """Move an entry to a different parent folder. **Two-step: preview, then execute.**

    Changes ``fullPath`` (folders carry their whole subtree); external
    references break. The path fence checks BOTH source and destination —
    the destination is re-checked on the execute leg.

    Step 1: call without ``confirmation_token`` -> preview with
    ``current_full_path``, ``would_be_full_path``, 5-minute token. Surface
    to the user. Step 2: re-call with the token; optional ``new_name``
    renames in the same operation.

    Pre-server errors: ``path_not_allowed`` (source or destination),
    ``invalid_confirmation_token``. Server slugs: ``not_found``,
    ``auth_failed``.
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
    # a denied folder is still a write-into-deny-zone. This fence runs on
    # both legs; the guarantee that the execute leg lands in the SAME
    # destination the user previewed comes from the token's parameter
    # binding on (new_parent_id, new_name), checked below.
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
        params={"new_parent_id": new_parent_id, "new_name": new_name},
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
