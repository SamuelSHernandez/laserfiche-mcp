"""Field, tag, and link write tools (set/merge variants).

These five tools are paired ``set_*`` (overwrite) / ``merge_*`` (delta)
operations on the field/tag/link metadata that lives directly on an entry.
None of them create or delete entries themselves — they mutate metadata.
"""

from __future__ import annotations

from typing import Any

from .. import _app
from ..errors import LaserficheError, classify_lf_error
from ._helpers import (
    ToolAbortedError,
    check_write_for_entry,
    fields_to_put_body,
    require_writes_enabled,
    user_fields_to_values,
)
from ._registry import register
from ._validators import (
    validate_field_names,
    validate_link_types,
    validate_tag_names,
)


@register(v2_name="laserfiche_field_set", is_write=True)
async def set_fields(
    entry_id: int,
    fields: dict[str, list[Any]],
) -> dict[str, Any]:
    """OVERWRITE all field values on an entry. Destructive — read carefully.

    **Prefer ``merge_fields``** for any "set field X to Y" intent. This
    tool follows the raw ``PUT /fields`` semantics: any field on the
    entry that is NOT in ``fields`` is deleted (independent fields) or
    reset to empty (templated fields). Use this only when you want that
    delete-everything-else behavior explicitly — e.g. clearing a stale
    snapshot before assigning a new one.

    Args:
        entry_id: Integer entry ID to write to.
        fields: Mapping of field name → list of values. Single-value
            fields take a one-item list; multi-value fields take many.
            Example: ``{"Last Name": ["Smith"], "Hire Date": ["2024-01-15"]}``.
            To clear a field, pass an empty list (``"Note": []``).

    Returns: The server's updated field listing on success.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry's path falls outside
          ``LF_WRITE_PATHS_ALLOW`` / inside ``LF_WRITE_PATHS_DENY``.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``required_field_missing``
    (you're trying to clear a required field), ``auth_failed``,
    ``not_found``.
    """
    require_writes_enabled()
    try:
        await check_write_for_entry("set_fields", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    name_err = await validate_field_names("set_fields", entry_id, list(fields.keys()))
    if name_err is not None:
        return name_err
    body = user_fields_to_values(fields)
    try:
        raw = await _app.get_client().put_fields(entry_id, body)
    except LaserficheError as exc:
        return classify_lf_error("set_fields", exc, entry_id=entry_id)
    return raw


@register(v2_name="laserfiche_field_merge", is_write=True)
async def merge_fields(
    entry_id: int,
    updates: dict[str, list[Any]],
) -> dict[str, Any]:
    """Update specific fields on an entry, preserving the rest.

    **The right default for "set field X to Y" intents.** This GET-then-PUT
    helper reads the entry's current field values, layers ``updates`` on
    top, and PUTs the union — fields not mentioned in ``updates`` keep
    their existing values. Use ``set_fields`` only when you specifically
    want overwrite-everything-else semantics.

    Args:
        entry_id: Integer entry ID to update.
        updates: Mapping of field name → list of values. Single-value
            fields take a one-item list; multi-value fields take many.
            Example: ``{"Last Name": ["Smith"], "Hire Year": ["2025"]}``.
            Pass an empty list (``"Note": []``) to clear a specific
            field while leaving others alone.

    Returns: ``{"mode": "executed", "operation": "merge_fields",
    "entry_id": <int>, "fields_updated": [...], "fields_preserved": [...],
    "result": <server response>}``. The ``fields_updated`` and
    ``fields_preserved`` arrays make it easy to confirm exactly what
    changed.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry's path falls outside the
          ``LF_WRITE_PATHS_ALLOW`` / inside ``LF_WRITE_PATHS_DENY``.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``,
    ``required_field_missing`` (clearing a required field),
    ``auth_failed``.
    """
    require_writes_enabled()
    try:
        await check_write_for_entry("merge_fields", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    name_err = await validate_field_names("merge_fields", entry_id, list(updates.keys()))
    if name_err is not None:
        return name_err
    client = _app.get_client()
    try:
        current = await client.get_field_values(entry_id)
    except LaserficheError as exc:
        return classify_lf_error("merge_fields", exc, entry_id=entry_id)

    items = current.get("value") or current.get("Value") or []
    body = fields_to_put_body(items)
    body.update(user_fields_to_values(updates))
    try:
        raw = await client.put_fields(entry_id, body)
    except LaserficheError as exc:
        return classify_lf_error("merge_fields", exc, entry_id=entry_id)
    return {
        "mode": "executed",
        "operation": "merge_fields",
        "entry_id": entry_id,
        "fields_updated": list(updates.keys()),
        "fields_preserved": [k for k in body if k not in updates],
        "result": raw,
    }


@register(v2_name="laserfiche_tag_set", is_write=True)
async def set_tags(
    entry_id: int,
    tags: list[str],
) -> dict[str, Any]:
    """OVERWRITE the tag list on an entry. Destructive — read carefully.

    Any tag currently on the entry that is NOT in ``tags`` will be
    removed. **Prefer ``merge_tags``** for additive intents — it adds
    and removes specific tags without disturbing the rest.

    Tags must already exist as repository-level tag definitions (see
    ``list_tag_definitions``); the server rejects unknown tag names.

    Args:
        entry_id: Integer entry ID.
        tags: Full list of tag names that should be on the entry after
            this call. Pass ``[]`` to clear all tags.

    Returns: The server's updated tag listing.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``,
    ``auth_failed``, ``server_error`` (typically caused by a tag name
    that isn't a defined tag — run ``list_tag_definitions`` first).
    """
    require_writes_enabled()
    try:
        await check_write_for_entry("set_tags", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    tag_err = await validate_tag_names("set_tags", entry_id, tags)
    if tag_err is not None:
        return tag_err
    try:
        raw = await _app.get_client().put_tags(entry_id, tags)
    except LaserficheError as exc:
        return classify_lf_error("set_tags", exc, entry_id=entry_id)
    return raw


@register(v2_name="laserfiche_tag_merge", is_write=True)
async def merge_tags(
    entry_id: int,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """Add and/or remove specific tags on an entry, preserving the rest.

    GET-then-PUT helper: reads the entry's current tags, applies
    ``add`` and ``remove`` deltas, and PUTs the union. Tags in
    ``remove`` that aren't currently on the entry are silently ignored.
    Tags in ``add`` that are already on the entry are no-ops.

    Tags must already exist as repository-level tag definitions (see
    ``list_tag_definitions``).

    Args:
        entry_id: Integer entry ID.
        add: Tag names to add. Default empty.
        remove: Tag names to remove. Default empty.

    Returns: ``{"mode": "executed", "operation": "merge_tags",
    "entry_id": <int>, "added": [...], "removed": [...],
    "final_tags": [...], "result": <server response>}``. ``added``
    and ``removed`` reflect what actually changed (excluding no-ops),
    so the LLM can confirm the delta.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — entry outside the allow list.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found``, ``auth_failed``.
    """
    require_writes_enabled()
    try:
        await check_write_for_entry("merge_tags", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    tag_err = await validate_tag_names(
        "merge_tags",
        entry_id,
        list((add or []) + (remove or [])),
    )
    if tag_err is not None:
        return tag_err
    client = _app.get_client()
    try:
        current = await client.get_tags(entry_id)
    except LaserficheError as exc:
        return classify_lf_error("merge_tags", exc, entry_id=entry_id)
    items = current.get("value") or current.get("Value") or []
    existing = {(t.get("name") or t.get("Name")) for t in items if (t.get("name") or t.get("Name"))}
    add_set = set(add or [])
    remove_set = set(remove or [])
    final = (existing | add_set) - remove_set
    try:
        raw = await client.put_tags(entry_id, sorted(final))
    except LaserficheError as exc:
        return classify_lf_error("merge_tags", exc, entry_id=entry_id)
    return {
        "mode": "executed",
        "operation": "merge_tags",
        "entry_id": entry_id,
        "added": sorted(add_set - existing),
        "removed": sorted(remove_set & existing),
        "final_tags": sorted(final),
        "result": raw,
    }


@register(v2_name="laserfiche_link_set", is_write=True)
async def set_links(
    entry_id: int,
    links: list[dict[str, Any]],
) -> dict[str, Any]:
    """OVERWRITE the entry-link list on an entry. Destructive — read carefully.

    Any link currently on the entry that is NOT in ``links`` will be
    removed. There is no ``merge_links`` helper — if you only want to
    add a link, first call ``get_entry`` (or inspect via the web client)
    to read the existing links, then call this with the full set.

    Args:
        entry_id: Integer entry ID — the source of each link.
        links: List of link descriptors. Each item is
            ``{"targetId": <int>, "linkTypeId": <int>}`` where
            ``linkTypeId`` comes from ``list_link_definitions`` (the
            ``Supersedes`` type, ``Attachment``, etc.) and ``targetId``
            is the other entry's ID. Pass ``[]`` to clear all links.

    Returns: The server's updated link listing.

    Pre-server errors (returned before the API call):
        - ``path_not_allowed`` — source entry outside the allow list.
          (Targets aren't fenced — the link metadata lives on the
          source, not the target.)

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found`` (source or
    target doesn't exist), ``auth_failed``, ``server_error`` (typically
    an invalid ``linkTypeId``).
    """
    require_writes_enabled()
    try:
        await check_write_for_entry("set_links", entry_id)
    except ToolAbortedError as aborted:
        return aborted.payload
    link_type_ids: list[int] = []
    for link in links:
        lid = link.get("linkTypeId")
        if isinstance(lid, int):
            link_type_ids.append(lid)
    link_err = await validate_link_types("set_links", entry_id, link_type_ids)
    if link_err is not None:
        return link_err
    try:
        raw = await _app.get_client().put_links(entry_id, links)
    except LaserficheError as exc:
        return classify_lf_error("set_links", exc, entry_id=entry_id)
    return raw
