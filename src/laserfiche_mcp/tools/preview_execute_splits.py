"""Preview/execute split tools for the 5 destructive multiplex operations.

The original v1.2+ destructive tools (``rename_entry``, ``move_entry``,
``delete_entry``, ``delete_edoc``, ``delete_pages``) multiplex preview
and execute behavior into a single tool — preview when
``confirmation_token`` is ``None``, execute when supplied. That works,
but it pushes the "did I just preview or actually do the thing?"
distinction into a runtime argument, which is harder for an LLM to
reason about than two distinctly named tools.

Per PLAN.md step 4, these splits register two new tools per operation:

  * ``..._preview`` — refuses ``confirmation_token`` and always returns
    the preview shape. Use this when you want to show the user what
    would happen.
  * ``..._execute`` — requires ``confirmation_token`` and runs the
    operation. Use this when you have the token from a previous
    preview call and the user has confirmed.

The originals remain registered as deprecation peers — the LLM can
use either path, and existing integrations don't break.

Implementation: each split is a thin wrapper that delegates to the
multiplex tool with the right argument. The ContextVar-based
``request_id`` propagates through, so the per-tool-call log line
emitted for the split also covers the inner delegation cleanly.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ._registry import register
from .writes_delete_edoc_pages import delete_edoc, delete_pages
from .writes_delete_entry import delete_entry
from .writes_move_rename import move_entry, rename_entry

# Shared annotations — keep the split tools' schemas consistent with their
# multiplex counterparts.
_ENTRY_ID = Annotated[int, Field(description="Integer entry ID.", ge=1)]
_PREVIEW_TOKEN = Annotated[
    str | None,
    Field(
        default=None,
        description=(
            "MUST be None on preview tools. Passing a token to a "
            "*_preview tool returns preview_does_not_accept_token; use "
            "the matching *_execute tool instead."
        ),
    ),
]
_EXECUTE_TOKEN = Annotated[
    str,
    Field(
        description=(
            "The HMAC-signed token returned by the matching *_preview "
            "tool. 5-minute TTL, bound to (operation, entry_id, "
            "entry_name)."
        ),
        min_length=1,
    ),
]
_NEW_NAME = Annotated[
    str,
    Field(
        description="New name. Path-safe (no backslashes). Max 128 chars.",
        examples=["report-final.pdf"],
        min_length=1,
        max_length=128,
    ),
]
_PAGE_RANGE = Annotated[
    str,
    Field(
        description=(
            "Page-range expression. Empty is refused — pass an explicit "
            "wide range like '1-9999' to delete every page."
        ),
        examples=["1,2,3", "1-3,5", "1-9999"],
        min_length=1,
    ),
]


def _reject_token_on_preview(operation: str, token: str | None) -> dict[str, Any] | None:
    """If ``token`` is set on a preview-only tool, return a structured refusal.

    Returning the token-bearing call to a ``..._preview`` tool would
    silently turn into an execute (because the multiplex tool branches on
    token presence). Refuse explicitly so the LLM understands the split.
    """
    if token is None:
        return None
    return {
        "mode": "error",
        "operation": operation,
        "kind": "invalid_input",
        "error": "preview_does_not_accept_token",
        "reason": (
            f"{operation} is the preview-only variant; it does not accept "
            "a confirmation_token. Call the *_execute variant with the "
            "token, or call the multiplex tool (without the _preview "
            "suffix) to get the legacy combined behavior."
        ),
    }


def _require_token_on_execute(operation: str, token: str | None) -> dict[str, Any] | None:
    """If ``token`` is missing or empty on an execute-only tool, return a structured refusal.

    Treats both ``None`` and the empty string as missing — agents
    sometimes default-construct empty strings for "optional" params, and
    an empty string is never a valid HMAC token.
    """
    if token:
        return None
    return {
        "mode": "error",
        "operation": operation,
        "kind": "invalid_input",
        "error": "execute_requires_token",
        "reason": (
            f"{operation} is the execute-only variant; it requires a "
            "confirmation_token obtained from the matching *_preview "
            "tool. Call the *_preview tool first, surface the preview to "
            "the user, then re-call this tool with the token."
        ),
    }


# --- rename_entry ------------------------------------------------------------


@register(v2_name="laserfiche_entry_rename_preview", is_write=True)
async def rename_entry_preview(
    entry_id: _ENTRY_ID,
    new_name: _NEW_NAME,
    confirmation_token: _PREVIEW_TOKEN = None,
) -> dict[str, Any]:
    """Preview a rename without executing it. Returns the diff + a token.

    See ``rename_entry`` for the full contract — this is the preview-only
    half. Always returns ``{"mode": "preview", ...,
    "confirmation_token": <str>}`` (or a structured error if the entry
    isn't found / the path is fenced). Call ``rename_entry_execute``
    with the same arguments + the returned token to actually rename.

    Args:
        entry_id: Integer entry ID to rename.
        new_name: New name to apply. Path-safe (no backslashes).
        confirmation_token: MUST be ``None``. Passing a token to this
            tool returns ``preview_does_not_accept_token`` — use the
            execute variant instead.
    """
    refusal = _reject_token_on_preview("rename_entry_preview", confirmation_token)
    if refusal is not None:
        refusal["entry_id"] = entry_id
        return refusal
    return await rename_entry(entry_id=entry_id, new_name=new_name)


@register(v2_name="laserfiche_entry_rename_execute", is_write=True)
async def rename_entry_execute(
    entry_id: _ENTRY_ID,
    new_name: _NEW_NAME,
    confirmation_token: _EXECUTE_TOKEN,
) -> dict[str, Any]:
    """Execute a previously-previewed rename. Requires the preview's token.

    Args:
        entry_id: Same value passed to ``rename_entry_preview``.
        new_name: Same value passed to ``rename_entry_preview``.
        confirmation_token: The HMAC-signed token returned by
            ``rename_entry_preview``. 5-minute TTL, bound to
            ``(operation, entry_id, current_name)``.
    """
    refusal = _require_token_on_execute("rename_entry_execute", confirmation_token)
    if refusal is not None:
        refusal["entry_id"] = entry_id
        return refusal
    return await rename_entry(
        entry_id=entry_id,
        new_name=new_name,
        confirmation_token=confirmation_token,
    )


# --- move_entry --------------------------------------------------------------


@register(v2_name="laserfiche_entry_move_preview", is_write=True)
async def move_entry_preview(
    entry_id: _ENTRY_ID,
    new_parent_id: Annotated[
        int,
        Field(description="Integer entry ID of the destination folder.", ge=1),
    ],
    new_name: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional rename to apply in the same operation.",
            min_length=1,
            max_length=128,
        ),
    ] = None,
    confirmation_token: _PREVIEW_TOKEN = None,
) -> dict[str, Any]:
    """Preview a move (and optional same-op rename). Returns the diff + a token.

    See ``move_entry`` for the full contract. The path-fence check runs
    on both source AND destination at preview time so a token issued
    here can't be replayed to land in a denied folder.
    """
    refusal = _reject_token_on_preview("move_entry_preview", confirmation_token)
    if refusal is not None:
        refusal["entry_id"] = entry_id
        refusal["new_parent_id"] = new_parent_id
        return refusal
    return await move_entry(
        entry_id=entry_id,
        new_parent_id=new_parent_id,
        new_name=new_name,
    )


@register(v2_name="laserfiche_entry_move_execute", is_write=True)
async def move_entry_execute(
    entry_id: _ENTRY_ID,
    new_parent_id: Annotated[
        int,
        Field(description="Integer entry ID of the destination folder.", ge=1),
    ],
    confirmation_token: _EXECUTE_TOKEN,
    new_name: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional rename to apply in the same operation.",
            min_length=1,
            max_length=128,
        ),
    ] = None,
) -> dict[str, Any]:
    """Execute a previously-previewed move. Requires the preview's token.

    Path-fence re-checks the destination on this call as well — a token
    issued for one destination cannot be replayed to a different one.
    """
    refusal = _require_token_on_execute("move_entry_execute", confirmation_token)
    if refusal is not None:
        refusal["entry_id"] = entry_id
        refusal["new_parent_id"] = new_parent_id
        return refusal
    return await move_entry(
        entry_id=entry_id,
        new_parent_id=new_parent_id,
        new_name=new_name,
        confirmation_token=confirmation_token,
    )


# --- delete_entry ------------------------------------------------------------


@register(v2_name="laserfiche_entry_delete_preview", is_write=True)
async def delete_entry_preview(
    entry_id: _ENTRY_ID,
    audit_reason_id: Annotated[
        int | None,
        Field(
            default=None,
            description="Required when LF_REQUIRE_AUDIT_REASON=true.",
            ge=1,
        ),
    ] = None,
    force_large_delete: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Required on execute when the folder's child count "
                "exceeds LF_DELETE_FOLDER_MAX_DESCENDANTS. Tracked here "
                "for symmetry with execute, ignored on preview."
            ),
        ),
    ] = False,
    confirmation_token: _PREVIEW_TOKEN = None,
) -> dict[str, Any]:
    """Preview a delete (folder cascade or document). Returns child count + token.

    For folders, surfaces ``immediate_child_count`` and
    ``exceeds_batch_cap``. If the count exceeds
    ``LF_DELETE_FOLDER_MAX_DESCENDANTS``, the execute leg must be called
    with ``force_large_delete=true`` alongside the token.
    """
    refusal = _reject_token_on_preview("delete_entry_preview", confirmation_token)
    if refusal is not None:
        refusal["entry_id"] = entry_id
        return refusal
    return await delete_entry(
        entry_id=entry_id,
        audit_reason_id=audit_reason_id,
        force_large_delete=force_large_delete,
    )


@register(v2_name="laserfiche_entry_delete_execute", is_write=True)
async def delete_entry_execute(
    entry_id: _ENTRY_ID,
    confirmation_token: _EXECUTE_TOKEN,
    audit_reason_id: Annotated[
        int | None,
        Field(
            default=None,
            description="Required when LF_REQUIRE_AUDIT_REASON=true.",
            ge=1,
        ),
    ] = None,
    force_large_delete: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Required when the folder's child count exceeds "
                "LF_DELETE_FOLDER_MAX_DESCENDANTS. Must be explicitly "
                "set to True for large folder deletes."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Execute a previously-previewed delete. Requires the preview's token."""
    refusal = _require_token_on_execute("delete_entry_execute", confirmation_token)
    if refusal is not None:
        refusal["entry_id"] = entry_id
        return refusal
    return await delete_entry(
        entry_id=entry_id,
        confirmation_token=confirmation_token,
        audit_reason_id=audit_reason_id,
        force_large_delete=force_large_delete,
    )


# --- delete_edoc -------------------------------------------------------------


@register(v2_name="laserfiche_document_edoc_delete_preview", is_write=True)
async def delete_edoc_preview(
    entry_id: _ENTRY_ID,
    confirmation_token: _PREVIEW_TOKEN = None,
) -> dict[str, Any]:
    """Preview an edoc delete (wipes the electronic-document content).

    Returns the entry metadata + the token. The entry itself and its
    template/field metadata remain after execute; only the edoc bytes
    go away.
    """
    refusal = _reject_token_on_preview("delete_edoc_preview", confirmation_token)
    if refusal is not None:
        refusal["entry_id"] = entry_id
        return refusal
    return await delete_edoc(entry_id=entry_id)


@register(v2_name="laserfiche_document_edoc_delete_execute", is_write=True)
async def delete_edoc_execute(
    entry_id: _ENTRY_ID,
    confirmation_token: _EXECUTE_TOKEN,
) -> dict[str, Any]:
    """Execute a previously-previewed edoc delete. Requires the preview's token."""
    refusal = _require_token_on_execute("delete_edoc_execute", confirmation_token)
    if refusal is not None:
        refusal["entry_id"] = entry_id
        return refusal
    return await delete_edoc(
        entry_id=entry_id,
        confirmation_token=confirmation_token,
    )


# --- delete_pages ------------------------------------------------------------


@register(v2_name="laserfiche_document_pages_delete_preview", is_write=True)
async def delete_pages_preview(
    entry_id: _ENTRY_ID,
    page_range: _PAGE_RANGE,
    confirmation_token: _PREVIEW_TOKEN = None,
) -> dict[str, Any]:
    """Preview a page-range delete from a document. Returns range + token.

    Empty ``page_range`` is refused with ``page_range_required`` — passing
    an empty string would mean "delete every page" and is intentionally
    not addressable through this API.
    """
    refusal = _reject_token_on_preview("delete_pages_preview", confirmation_token)
    if refusal is not None:
        refusal["entry_id"] = entry_id
        return refusal
    return await delete_pages(entry_id=entry_id, page_range=page_range)


@register(v2_name="laserfiche_document_pages_delete_execute", is_write=True)
async def delete_pages_execute(
    entry_id: _ENTRY_ID,
    page_range: _PAGE_RANGE,
    confirmation_token: _EXECUTE_TOKEN,
) -> dict[str, Any]:
    """Execute a previously-previewed page-range delete."""
    refusal = _require_token_on_execute("delete_pages_execute", confirmation_token)
    if refusal is not None:
        refusal["entry_id"] = entry_id
        return refusal
    return await delete_pages(
        entry_id=entry_id,
        page_range=page_range,
        confirmation_token=confirmation_token,
    )
