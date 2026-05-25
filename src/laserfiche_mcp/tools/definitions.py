"""Repository-level definition listings: fields, tags, templates, links, audit reasons."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .. import _app
from .._app import clamp_max_results, get_settings
from ..errors import LaserficheError, classify_lf_error
from ._registry import register

# Shared field annotations for the four list_*_definitions tools so the
# JSON schema the LLM sees stays consistent across them.
_DEF_MAX_RESULTS = Annotated[
    int | None,
    Field(
        default=None,
        description="Page size (default 25, capped by LF_MAX_RESULTS_CEILING).",
        ge=1,
        le=1000,
    ),
]
_DEF_SKIP = Annotated[
    int,
    Field(
        default=0,
        description="0-indexed offset for pagination through large repositories.",
        ge=0,
    ),
]
_DEF_SUMMARY_ONLY = Annotated[
    bool,
    Field(
        default=False,
        description=(
            "When True, return only {count, names} instead of the full "
            "OData listing — useful for 'what's available?' lookups that "
            "would otherwise return 30-50 KB of definition payload."
        ),
    ),
]


@register(v2_name="laserfiche_repository_list")
async def list_repositories() -> dict[str, Any]:
    """List the repositories this account can reach on the server.

    Useful for confirming which repository the server is pointed at and
    for discovering alternate repositories the same account can access.

    **Endpoint variability**: some self-hosted Laserfiche builds disable
    the ``/Repositories`` endpoint entirely. When the call fails, this
    tool does NOT raise — it returns the configured repo as a fallback
    so downstream tools can still run. Branch on ``mode == "fallback"``
    if you need to distinguish a partial answer from a full enumeration.

    Returns: On a healthy build, the server's raw OData listing with
    ``value``: ``[{repoId, displayName, ...}, ...]``. On endpoint
    failure: ``{"mode": "fallback", "warning": <str>, "server_error":
    <classified error>, "value": [{"repoId": "<LF_REPOSITORY_ID>",
    "displayName": null, "is_configured": true}]}``.

    On failure: this tool never raises and never returns ``mode:
    "error"`` — see the fallback shape above.
    """
    try:
        raw = await _app.get_client().list_repositories()
    except LaserficheError as exc:
        settings = get_settings()
        return {
            "mode": "fallback",
            "operation": "list_repositories",
            "warning": (
                f"Server's /Repositories endpoint returned an error "
                f"(status={exc.status_code}). Returning the configured "
                f"repository from LF_REPOSITORY_ID; other repos on this "
                f"server are not enumerable from this build."
            ),
            "server_error": classify_lf_error("list_repositories", exc),
            "value": [
                {
                    "repoId": settings.repository_id,
                    "displayName": None,
                    "is_configured": True,
                }
            ],
        }
    return raw


def _summarize_definition_list(raw: dict[str, Any]) -> dict[str, Any]:
    """Return ``{count, names}`` for a definitions listing.

    Used by the four ``list_*_definitions`` tools when ``summary_only=True``.
    Reduces a 30–50 KB payload to a tiny one for "what's available?" workflows.
    """
    items = raw.get("value") or []
    names = [item.get("name") or item.get("displayName") or "" for item in items]
    return {"count": len(names), "names": [n for n in names if n]}


@register(v2_name="laserfiche_field_definition_list")
async def list_field_definitions(
    max_results: _DEF_MAX_RESULTS = None,
    skip: _DEF_SKIP = 0,
    *,
    summary_only: _DEF_SUMMARY_ONLY = False,
) -> dict[str, Any]:
    """List every field definition in the repository.

    Use before authoring a field-based search query or preparing a field
    update — the response tells you which fields exist, their types
    (``String``, ``ShortInteger``, ``List``, ``Date``, ...), whether they
    accept multi-value, whether they're required at the repository level,
    and (for ``List`` fields) the allowed values.

    Independent fields and template-scoped fields are both returned.
    Combine with ``list_template_definitions`` to see which fields belong
    to which template.

    Args:
        max_results: Page size (default 25, capped by ``LF_MAX_RESULTS_CEILING``).
        skip: 0-indexed offset for pagination through large repositories.
        summary_only: If True, return only ``{count, names}`` instead of the
            full OData listing.

    Returns: Server's raw OData listing with ``value`` (list of field
    definitions). Each item includes ``id``, ``name``, ``fieldType``,
    ``isRequired``, ``isMultiValue``, ``listValues``, ``defaultValue``,
    ``length``, ``constraint``.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    """
    try:
        raw = await _app.get_client().list_field_definitions(
            max_results=clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        return classify_lf_error("list_field_definitions", exc)
    if summary_only:
        return _summarize_definition_list(raw)
    return raw


@register(v2_name="laserfiche_tag_definition_list")
async def list_tag_definitions(
    max_results: _DEF_MAX_RESULTS = None,
    skip: _DEF_SKIP = 0,
    *,
    summary_only: _DEF_SUMMARY_ONLY = False,
) -> dict[str, Any]:
    """List every tag definition in the repository.

    Use before calling ``set_tags`` / ``merge_tags`` to confirm a tag
    exists — the server rejects tags that aren't defined here. Tags are
    a flat namespace in Laserfiche, distinct from template fields.

    Args:
        max_results: Page size (default 25, capped by ``LF_MAX_RESULTS_CEILING``).
        skip: 0-indexed offset for pagination.
        summary_only: If True, return only ``{count, names}``.

    Returns: Server's raw OData listing with ``value`` (list of tag
    definitions). Each item has ``id``, ``name``, and ``isSecurityTag``.
    Many repositories ship with no tags defined; an empty ``value`` is
    normal.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    """
    try:
        raw = await _app.get_client().list_tag_definitions(
            max_results=clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        return classify_lf_error("list_tag_definitions", exc)
    if summary_only:
        return _summarize_definition_list(raw)
    return raw


@register(v2_name="laserfiche_template_definition_list")
async def list_template_definitions(
    template_name: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "If set, return only the template with this exact name. "
                "Case-sensitive on most builds."
            ),
            examples=["Personnel Document", "Loan Application"],
        ),
    ] = None,
    max_results: _DEF_MAX_RESULTS = None,
    skip: _DEF_SKIP = 0,
    *,
    summary_only: _DEF_SUMMARY_ONLY = False,
) -> dict[str, Any]:
    """List template definitions in the repository.

    Use to discover which templates exist before calling ``assign_template``.
    Pass ``template_name`` to fetch a single template by name (the same
    listing, filtered server-side).

    Args:
        template_name: If set, return only the template with this exact
            name. Case-sensitive on most builds.
        max_results: Page size (default 25, capped by ``LF_MAX_RESULTS_CEILING``).
        skip: 0-indexed offset for pagination.
        summary_only: If True, return only ``{count, names}``.

    Returns: Server's raw OData listing with ``value``. Each item has
    ``id``, ``name``, ``displayName``, ``description``, ``fieldCount``,
    and ``color``. This response does NOT enumerate the fields ON the
    template — use ``list_field_definitions`` to inspect those (they're
    the ones with ``isRequired=true`` when scoped to the template).

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    """
    try:
        raw = await _app.get_client().list_template_definitions(
            template_name=template_name,
            max_results=clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        return classify_lf_error("list_template_definitions", exc)
    if summary_only:
        return _summarize_definition_list(raw)
    return raw


@register(v2_name="laserfiche_template_field_list")
async def get_template_fields(
    template_name: Annotated[
        str,
        Field(
            description=(
                "Exact template name (case-sensitive on most builds). Use "
                "list_template_definitions to discover available names."
            ),
            examples=["Personnel Document", "Loan Application", "Invoice"],
        ),
    ],
    *,
    required_only: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "When True, return only fields where is_required is true — "
                "useful for 'what's the minimum I have to supply?' workflows."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Return the fields belonging to a single template, with full field metadata.

    Closes the most common pre-assign workflow gap: instead of fetching
    ``list_template_definitions`` then ``list_field_definitions`` and
    cross-referencing client-side, this returns the template's field
    list directly with each field's type, constraints, and required
    flag inlined. Use this BEFORE ``assign_template`` to construct the
    ``fields`` argument.

    Args:
        template_name: Exact template name (case-sensitive on most
            builds). Use ``list_template_definitions`` to discover
            available names.
        required_only: When ``True``, return only fields where
            ``is_required`` is true. Useful for "what's the minimum I
            have to supply?" workflows.

    Returns: ``{"template_name": <str>, "template_id": <int>,
    "field_count": <int>, "fields": [...]}`` where each field has
    ``name``, ``field_type``, ``is_required``, ``is_multi_value``,
    ``list_values``, ``default_value``, ``length``, ``constraint``.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    Slugs: ``invalid_template_name`` when the template name doesn't
    exist in the repository (with the list of valid names in the
    response); ``server_error`` for upstream issues.
    """
    client = _app.get_client()
    try:
        template_defs = await client.cached_template_definitions()
    except LaserficheError as exc:
        return classify_lf_error(
            "get_template_fields",
            exc,
            extra={"template_name": template_name},
        )
    tpl = template_defs.get(template_name)
    if tpl is None:
        return {
            "mode": "error",
            "operation": "get_template_fields",
            "error": "invalid_template_name",
            "template_name": template_name,
            "reason": (
                f"Template {template_name!r} is not defined in this "
                "repository. Match is case-sensitive."
            ),
            "valid_template_names": sorted(template_defs.keys()),
        }
    template_field_names = tpl.get("templateFieldNames") or tpl.get("fieldNames") or []
    try:
        field_defs = await client.cached_field_definitions()
    except LaserficheError as exc:
        return classify_lf_error(
            "get_template_fields",
            exc,
            extra={"template_name": template_name},
        )
    fields_out: list[dict[str, Any]] = []
    for name in template_field_names:
        fd = field_defs.get(name)
        if fd is None:
            continue
        if required_only and not fd.get("isRequired"):
            continue
        fields_out.append(
            {
                "name": name,
                "field_id": fd.get("id"),
                "field_type": fd.get("fieldType"),
                "is_required": bool(fd.get("isRequired")),
                "is_multi_value": bool(fd.get("isMultiValue")),
                "list_values": fd.get("listValues") or [],
                "default_value": fd.get("defaultValue"),
                "length": fd.get("length"),
                "constraint": fd.get("constraint"),
            }
        )
    return {
        "template_name": template_name,
        "template_id": tpl.get("id"),
        "field_count": len(fields_out),
        "fields": fields_out,
    }


@register(v2_name="laserfiche_link_definition_list")
async def list_link_definitions(
    max_results: _DEF_MAX_RESULTS = None,
    skip: _DEF_SKIP = 0,
    *,
    summary_only: _DEF_SUMMARY_ONLY = False,
) -> dict[str, Any]:
    """List the entry-link type definitions available on this repository.

    Use before calling ``set_links`` — you need a ``linkTypeId`` from
    this listing to construct a valid link. Each link type is directed:
    it has a ``sourceLabel`` (how the relationship reads from the source
    entry) and a ``targetLabel`` (how it reads from the target).

    Args:
        max_results: Page size (default 25).
        skip: 0-indexed offset for pagination.
        summary_only: If True, return only ``{count, names}``.

    Returns: Server's raw OData listing with ``value``. Each item has
    ``linkTypeId``, ``sourceLabel``, ``targetLabel``, and
    ``linkTypeDescription``. Common defaults include ``"Supersedes" /
    "Superseded by"`` and ``"Attachment" / "Message"``.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    """
    try:
        raw = await _app.get_client().list_link_definitions(
            max_results=clamp_max_results(max_results),
            skip=max(0, skip),
        )
    except LaserficheError as exc:
        return classify_lf_error("list_link_definitions", exc)
    if summary_only:
        return _summarize_definition_list(raw)
    return raw


@register(v2_name="laserfiche_audit_reason_list")
async def get_audit_reasons() -> dict[str, Any]:
    """Return the audit-reason codes the authenticated user is allowed to supply.

    Use before ``delete_entry`` or ``get_document_edoc`` (with export
    auditing) when ``LF_REQUIRE_AUDIT_REASON=true`` or when the user is
    asking for an audited delete. The response is grouped by operation
    type — pick an ID from the correct group.

    Returns: Dict shaped roughly as ``{"deleteEntry": [{id, name, ...}],
    "exportDocument": [...], ...}``. Each item has ``id``, ``name``, and
    ``description``. The ``id`` is what you pass to ``delete_entry`` as
    ``audit_reason_id``.

    On failure: returns ``{"mode": "error", "error": <slug>, ...}``.
    Common slugs: ``auth_failed`` if the account isn't permitted to audit.
    """
    try:
        raw = await _app.get_client().get_audit_reasons()
    except LaserficheError as exc:
        return classify_lf_error("get_audit_reasons", exc)
    return raw
