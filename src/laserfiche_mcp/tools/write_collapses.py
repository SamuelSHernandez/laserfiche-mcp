"""Collapsed write tools that subsume the v2.0 set/merge + assign/remove pairs.

Per PLAN.md step 3, these five tools combine the existing paired
operations into single LLM-facing names so the model only has to learn
"do you want to overwrite (mode='replace') or layer (mode='merge')?"
rather than picking between two differently-named tools.

  * ``field_update(entry_id, updates, mode="merge")`` — wraps
    ``merge_fields`` (default) and ``set_fields`` (``mode="replace"``).
  * ``tag_update(entry_id, add=None, remove=None, replace=None)`` —
    wraps ``merge_tags`` (when ``replace`` is None) and ``set_tags``
    (when ``replace`` is supplied, even if empty).
  * ``link_update(entry_id, links, mode="replace")`` — wraps
    ``set_links``. ``mode="merge"`` reads current links and unions them
    with the new list (server lacks a delta endpoint, so this is
    GET-then-PUT).
  * ``template_assign_or_remove(entry_id, template_name=None, fields=None)``
    — wraps ``assign_template`` (when ``template_name`` is set) and
    ``remove_template`` (when ``template_name`` is None).
  * ``task_wait_or_poll(operation_token, timeout_seconds=60)`` — wraps
    ``wait_for_task`` (default) and ``get_task_status`` (when
    ``timeout_seconds=0``).

The original tool names remain registered so existing integrations
don't break. Implementation here delegates rather than duplicating —
all the validation, fencing, and classify_lf_error wiring stays in
one place.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .. import _app
from ..errors import LaserficheError, classify_lf_error
from ._helpers import (
    ToolAbortedError,
    check_write_for_entry,
    require_writes_enabled,
)
from ._registry import register
from ._validators import validate_link_types
from .tasks import get_task_status, wait_for_task
from .writes_fields_tags_links import (
    merge_fields,
    merge_tags,
    set_fields,
    set_links,
    set_tags,
)
from .writes_templates import assign_template, remove_template


def _invalid_input(operation: str, error: str, reason: str, **extras: Any) -> dict[str, Any]:
    """Shared pre-server-guard error shape for the collapses."""
    payload: dict[str, Any] = {
        "mode": "error",
        "operation": operation,
        "kind": "invalid_input",
        "error": error,
        "reason": reason,
    }
    payload.update({k: v for k, v in extras.items() if v is not None})
    return payload


# --- field_update -----------------------------------------------------------


@register(v2_name="laserfiche_field_update", is_write=True)
async def field_update(
    entry_id: Annotated[
        int,
        Field(description="Integer entry ID.", ge=1),
    ],
    updates: Annotated[
        dict[str, list[Any]],
        Field(
            description=(
                "Mapping of field name → list of values. Single-value "
                "fields take a one-item list; multi-value take many. "
                "Pass an empty list (e.g. {'Note': []}) to clear a "
                "specific field — works in both modes."
            ),
            examples=[
                {"Status": ["Approved"]},
                {"Last Name": ["Smith"], "Hire Date": ["2024-01-15"]},
            ],
        ),
    ],
    mode: Annotated[
        Literal["merge", "replace"],
        Field(
            default="merge",
            description=(
                "'merge' (default, safer): leaves fields not in `updates` "
                "alone — right for 'set field X to Y' intents. "
                "'replace': clears any field not in `updates` — only when "
                "you want delete-everything-else semantics."
            ),
        ),
    ] = "merge",
) -> dict[str, Any]:
    """Update field values on an entry. Default merges; ``mode="replace"`` overwrites.

    The single field-write tool you should reach for. Wraps the two
    underlying paths:

    * ``mode="merge"`` (default, **safer**): leaves fields not mentioned
      in ``updates`` alone. Equivalent to ``merge_fields``. Right for
      "set field X to Y" intents.
    * ``mode="replace"``: clears any field not in ``updates``. Equivalent
      to ``set_fields``. Only use when you specifically want
      delete-everything-else semantics (e.g. wiping a stale template
      snapshot before re-applying).

    Args:
        entry_id: Integer entry ID.
        updates: Mapping of field name → list of values. Single-value
            fields take a one-item list; multi-value fields take many.
            Example: ``{"Last Name": ["Smith"], "Hire Date": ["2024-01-15"]}``.
            Pass an empty list (``"Note": []``) to clear that specific
            field — works in both modes.
        mode: ``"merge"`` (default) or ``"replace"``. Any other value
            returns ``invalid_mode``.

    Returns: On merge, ``{"mode": "executed", "operation": "merge_fields",
    "fields_updated": [...], "fields_preserved": [...], "result": ...}``.
    On replace, the server's raw updated field listing.

    On failure: same shapes as ``merge_fields`` / ``set_fields`` (kind +
    error subkind structured response).
    """
    if mode == "merge":
        return await merge_fields(entry_id=entry_id, updates=updates)
    if mode == "replace":
        return await set_fields(entry_id=entry_id, fields=updates)
    return _invalid_input(
        "field_update",
        "invalid_mode",
        f"mode must be 'merge' or 'replace', got {mode!r}.",
        entry_id=entry_id,
    )


# --- tag_update --------------------------------------------------------------


@register(v2_name="laserfiche_tag_update", is_write=True)
async def tag_update(
    entry_id: Annotated[
        int,
        Field(description="Integer entry ID.", ge=1),
    ],
    add: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Tags to add (merge semantics). Mutually exclusive with "
                "`replace`."
            ),
            examples=[["Confidential"]],
        ),
    ] = None,
    remove: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Tags to remove (merge semantics). Mutually exclusive "
                "with `replace`."
            ),
            examples=[["Draft"]],
        ),
    ] = None,
    replace: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Full tag list to set (replace semantics). Even an empty "
                "list ([]) switches into replace mode and clears all "
                "tags. Mutually exclusive with add/remove."
            ),
            examples=[["Confidential", "Q3-2024"], []],
        ),
    ] = None,
) -> dict[str, Any]:
    """Update tags on an entry. Provide ``replace`` for overwrite OR ``add``/``remove`` for delta.

    Wraps the two underlying tools:

    * **Merge semantics (default, safer):** pass ``add`` and/or
      ``remove``. Tags not mentioned stay as-is. Equivalent to
      ``merge_tags``. Right for "tag this entry as X" intents.
    * **Replace semantics:** pass ``replace`` (even ``replace=[]`` to
      clear all tags). Equivalent to ``set_tags``. Any tag currently on
      the entry that isn't in ``replace`` is removed.

    The two modes are mutually exclusive — passing ``replace`` together
    with ``add``/``remove`` returns ``conflicting_modes``.

    Args:
        entry_id: Integer entry ID.
        add: Tag names to add (merge mode).
        remove: Tag names to remove (merge mode).
        replace: Full tag list to set (replace mode). Even an empty
            list switches into replace mode.

    Returns: Same shapes as ``merge_tags`` / ``set_tags``.

    On failure: ``conflicting_modes`` if both ``replace`` and
    ``add``/``remove`` are passed; ``no_op`` if all three are None or
    empty; otherwise same shapes as the underlying tools.
    """
    using_replace = replace is not None
    using_delta = bool(add) or bool(remove)

    if using_replace and using_delta:
        return _invalid_input(
            "tag_update",
            "conflicting_modes",
            "Pass either `replace=` (overwrite) or `add=`/`remove=` (delta), "
            "not both.",
            entry_id=entry_id,
        )
    if not using_replace and not using_delta:
        return _invalid_input(
            "tag_update",
            "no_op",
            "Provide at least one of `add`, `remove`, or `replace`. "
            "Use `replace=[]` to explicitly clear all tags.",
            entry_id=entry_id,
        )

    if using_replace:
        return await set_tags(entry_id=entry_id, tags=replace or [])
    return await merge_tags(entry_id=entry_id, add=add, remove=remove)


# --- link_update -------------------------------------------------------------


@register(v2_name="laserfiche_link_update", is_write=True)
async def link_update(
    entry_id: Annotated[
        int,
        Field(description="Integer entry ID — the source of each link.", ge=1),
    ],
    links: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "List of {'targetId': <int>, 'linkTypeId': <int>} "
                "descriptors. linkTypeId comes from list_link_definitions."
            ),
            examples=[[{"targetId": 42, "linkTypeId": 1}]],
        ),
    ],
    mode: Annotated[
        Literal["replace", "merge"],
        Field(
            default="replace",
            description=(
                "'replace' (default): overwrites the link list. "
                "'merge': GET-then-PUT union (the server has no native "
                "delta endpoint)."
            ),
        ),
    ] = "replace",
) -> dict[str, Any]:
    """Update entry-links. Default replaces; ``mode="merge"`` adds without removing.

    Wraps ``set_links`` and adds a GET-then-PUT merge path on top because
    the server has no delta endpoint for links.

    * ``mode="replace"`` (default — matches the underlying
      ``set_links``): overwrites the link list. Any current link not in
      ``links`` is removed.
    * ``mode="merge"``: reads current links, unions them with ``links``
      (de-duplicated on ``(targetId, linkTypeId)``), and PUTs the
      result. Existing links not in ``links`` are preserved.

    Args:
        entry_id: Integer entry ID — the source of each link.
        links: List of ``{"targetId": <int>, "linkTypeId": <int>}``
            descriptors. ``linkTypeId`` from ``list_link_definitions``.
        mode: ``"replace"`` (default) or ``"merge"``. Any other value
            returns ``invalid_mode``.

    Returns: The server's updated link listing on success.

    On failure: same shapes as ``set_links``.
    """
    if mode == "replace":
        return await set_links(entry_id=entry_id, links=links)
    if mode != "merge":
        return _invalid_input(
            "link_update",
            "invalid_mode",
            f"mode must be 'replace' or 'merge', got {mode!r}.",
            entry_id=entry_id,
        )

    # Merge: GET current links → union → PUT. Apply the same path-fence
    # and link-type validation that set_links does.
    require_writes_enabled()
    try:
        await check_write_for_entry("link_update", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    link_type_ids = [
        link["linkTypeId"] for link in links if isinstance(link.get("linkTypeId"), int)
    ]
    link_err = await validate_link_types("link_update", entry_id, link_type_ids)
    if link_err is not None:
        return link_err

    client = _app.get_client()
    try:
        current = await client.get_links(entry_id)
    except LaserficheError as exc:
        return classify_lf_error("link_update", exc, entry_id=entry_id)

    items = current.get("value") or current.get("Value") or []
    seen: set[tuple[Any, Any]] = set()
    union: list[dict[str, Any]] = []
    for link in (*items, *links):
        target = link.get("targetId") or link.get("TargetId")
        link_type = link.get("linkTypeId") or link.get("LinkTypeId")
        key = (target, link_type)
        if key in seen:
            continue
        seen.add(key)
        # Normalize to camelCase for the PUT body shape set_links uses.
        union.append(
            {
                "targetId": key[0],
                "linkTypeId": key[1],
            }
        )
    try:
        raw = await client.put_links(entry_id, union)
    except LaserficheError as exc:
        return classify_lf_error("link_update", exc, entry_id=entry_id)
    return {
        "mode": "executed",
        "operation": "link_update",
        "entry_id": entry_id,
        "added_count": len(union) - len(items),
        "total_links": len(union),
        "result": raw,
    }


# --- template_assign_or_remove ----------------------------------------------


@register(v2_name="laserfiche_template_update", is_write=True)
async def template_assign_or_remove(
    entry_id: Annotated[
        int,
        Field(description="Integer entry ID.", ge=1),
    ],
    template_name: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Exact template name to assign, or None to clear the "
                "current assignment."
            ),
            examples=["Personnel Document", "Invoice"],
        ),
    ] = None,
    fields: Annotated[
        dict[str, list[Any]] | None,
        Field(
            default=None,
            description=(
                "Initial field values to set when assigning. Same shape "
                "as field_update's `updates`. Ignored — and an error "
                "returned — when template_name is None."
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """Assign a template (when ``template_name`` is set) or clear it (when None).

    Single tool for both halves of the template lifecycle:

    * ``template_name="Personnel Document"`` → ``assign_template``,
      including the repository-required-field validator. Pass ``fields``
      to set initial values in the same call.
    * ``template_name=None`` → ``remove_template``. Clears the template
      assignment; templated field values are wiped, independent fields
      survive. ``fields`` MUST be omitted in this case
      (``fields_ignored_on_remove`` otherwise).

    Args:
        entry_id: Integer entry ID.
        template_name: Exact template name to assign, or ``None`` to
            clear the current assignment.
        fields: Initial field values to set when assigning. Same shape
            as ``field_update``'s ``updates``. Ignored — and an error
            is returned — when ``template_name`` is None.

    Returns: Same shapes as ``assign_template`` / ``remove_template``.

    On failure: ``fields_ignored_on_remove`` if both ``template_name=None``
    and ``fields`` are set; otherwise same shapes as the underlying tools.
    """
    if template_name is None:
        if fields:
            return _invalid_input(
                "template_update",
                "fields_ignored_on_remove",
                "fields= is meaningful only when assigning a template; "
                "pass template_name= to assign, or omit fields= to clear.",
                entry_id=entry_id,
            )
        return await remove_template(entry_id=entry_id)
    return await assign_template(
        entry_id=entry_id,
        template_name=template_name,
        fields=fields,
    )


# --- task_wait_or_poll ------------------------------------------------------


@register(v2_name="laserfiche_task_update")
async def task_wait_or_poll(
    operation_token: Annotated[
        str,
        Field(
            description=(
                "Token from the originating async tool (delete_entry, "
                "copy_entry, occasionally import_document)."
            ),
            min_length=1,
        ),
    ],
    timeout_seconds: Annotated[
        int,
        Field(
            default=60,
            description=(
                "0 for single-poll (get_task_status semantics); >0 for "
                "blocking wait (wait_for_task semantics)."
            ),
            ge=0,
            le=3600,
        ),
    ] = 60,
    poll_interval_seconds: Annotated[
        float,
        Field(
            default=1.0,
            description=(
                "Delay between status checks when waiting. Bounded below "
                "at 0.1s. Ignored when timeout_seconds=0."
            ),
            ge=0.1,
            le=60.0,
        ),
    ] = 1.0,
) -> dict[str, Any]:
    """Check or wait on an async operation. ``timeout_seconds=0`` returns immediately.

    Wraps the two underlying tools:

    * ``timeout_seconds=0`` → ``get_task_status``. Returns the current
      payload without waiting. Right for "is this done yet?" polling
      loops written by the caller.
    * ``timeout_seconds>0`` (default 60) → ``wait_for_task``. Polls at
      ``poll_interval_seconds`` until terminal or until the deadline.

    Args:
        operation_token: Token from the originating async tool.
        timeout_seconds: ``0`` for single-poll; ``>0`` for blocking wait.
            Bounded above by what your MCP client tolerates as a tool
            call duration.
        poll_interval_seconds: Delay between status checks when waiting.
            Bounded below at 0.1s. Ignored when ``timeout_seconds=0``.

    Returns: Same payload as ``get_task_status`` / ``wait_for_task``.
    The wait variant adds ``timed_out: bool`` for deadline misses.

    On failure: same shapes as the underlying tools.
    """
    if timeout_seconds <= 0:
        return await get_task_status(operation_token=operation_token)
    return await wait_for_task(
        operation_token=operation_token,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
