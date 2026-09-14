"""``compare_entries`` — diff two entries' metadata and template fields.

This is the MCP wrapper. The actual diff logic lives in ``ops/compare.py``
so the ``laserfiche-mcp diff`` (alias ``compare``) subcommand runs the exact
same comparison without an LLM in the loop.

Shipping both entries' raw JSON into a context window and asking a model to
eyeball the differences is strictly worse than computing the diff: more
expensive, and occasionally wrong about which of forty fields actually
changed. This tool does the set comparison server-side and returns only
what differs.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from pydantic import Field

from .. import _app
from ..errors import LaserficheError, classify_lf_error
from ..ops import compare as compare_ops
from ._registry import register

__all__ = ["compare_entries"]


@register(v2_name="laserfiche_entry_compare")
async def compare_entries(
    left_entry_id: Annotated[
        int,
        Field(description="Entry ID of the first ('left') entry.", ge=1),
    ],
    right_entry_id: Annotated[
        int,
        Field(description="Entry ID of the second ('right') entry.", ge=1),
    ],
    include_fields: Annotated[
        bool,
        Field(
            default=True,
            description=(
                "Also compare template field values, not just entry "
                "attributes. Set false to skip the two extra get_field_values "
                "round-trips when only name/type/extension/page-count matter."
            ),
        ),
    ] = True,
) -> dict[str, Any]:
    """Diff two entries' attributes and (optionally) template field values.

    **Use this instead of fetching both entries yourself and comparing by
    eye** — it's a deterministic set comparison, not a judgment call, and it
    tells you exactly which of possibly dozens of fields disagree instead of
    making you scan two JSON blobs. Typical uses: "are these two copies of
    the contract actually the same?", "what changed between this record and
    the template version?", "did the import bring over every field?".

    Compares five entry attributes — ``name``, ``entryType``,
    ``templateName``, ``extension``, ``pageCount`` — plus, when
    ``include_fields`` is true (the default), every template field value on
    either entry. Timestamps and entry IDs are intentionally excluded: two
    copies of the same document differing only in creation time is noise,
    not a finding.

    A field present on one entry's template but absent on the other's is
    reported separately (``only_left_fields`` / ``only_right_fields``) from
    a field present on both with different values (``differences``) — "you
    forgot to fill this in" and "these disagree" are different problems.

    Sibling tools: ``get_entry`` / ``get_field_values`` to inspect one entry
    on its own; ``get_entry_by_path`` to resolve a path to the entry ID this
    tool needs when you only have a location, not an ID.

    Returns ``{"mode": "entry_comparison", "left_entry_id", "right_entry_id",
    "identical": bool, "differences": [{"kind": "attribute"|"field", "name",
    "left", "right"}, ...], "same": [<attribute/field names>],
    "only_left_fields": [...], "only_right_fields": [...]}``. On failure
    returns ``{"mode": "error", "error": <slug>, "entry_id": <int>,
    "side": "left"|"right"}`` — ``not_found`` (bad ID on either side),
    ``auth_failed``. This tool never writes to Laserfiche.
    """
    client = _app.get_client()

    try:
        left_entry = await client.get_entry(left_entry_id)
    except LaserficheError as exc:
        return classify_lf_error(
            "compare_entries", exc, entry_id=left_entry_id, extra={"side": "left"}
        )
    try:
        right_entry = await client.get_entry(right_entry_id)
    except LaserficheError as exc:
        return classify_lf_error(
            "compare_entries", exc, entry_id=right_entry_id, extra={"side": "right"}
        )

    left_fields: dict[str, Any] | None = None
    right_fields: dict[str, Any] | None = None
    if include_fields:
        try:
            left_fields = await client.get_field_values(left_entry_id)
        except LaserficheError as exc:
            return classify_lf_error(
                "compare_entries",
                exc,
                entry_id=left_entry_id,
                extra={"side": "left", "stage": "fields"},
            )
        try:
            right_fields = await client.get_field_values(right_entry_id)
        except LaserficheError as exc:
            return classify_lf_error(
                "compare_entries",
                exc,
                entry_id=right_entry_id,
                extra={"side": "right", "stage": "fields"},
            )

    result = compare_ops.compare_entries(
        left_entry,
        right_entry,
        left_fields=left_fields,
        right_fields=right_fields,
    )

    return {
        "mode": "entry_comparison",
        "left_entry_id": result.left_id,
        "right_entry_id": result.right_id,
        "identical": result.identical,
        "differences": [asdict(d) for d in result.differences],
        "same": result.same,
        "only_left_fields": result.only_left_fields,
        "only_right_fields": result.only_right_fields,
    }
