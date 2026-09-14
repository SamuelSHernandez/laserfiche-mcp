"""Repository-level definition listings: fields, tags, templates, links, audit reasons."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .. import _app
from .._app import clamp_max_results, get_settings
from ..errors import LaserficheError, classify_lf_error, local_error
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
        description="When True, return only {count, names} instead of the full listing.",
    ),
]


@register(v2_name="laserfiche_repository_list")
async def list_repositories() -> dict[str, Any]:
    """List the repositories this account can reach on the server.

    Never raises and never returns ``mode: "error"`` — some builds disable
    the ``/Repositories`` endpoint, in which case the configured repo comes
    back as ``{"mode": "fallback", "warning", "value": [...]}`` so
    downstream tools still run. Healthy builds return the raw OData listing.
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

    Use before authoring a field query or field update — returns each
    field's ``name``, ``fieldType``, ``isRequired``, ``isMultiValue``,
    ``listValues``, etc. For the fields on one template,
    ``get_template_fields`` is the direct route.

    On failure returns ``{"mode": "error", "error": <slug>}``.
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

    Use before ``set_tags``/``merge_tags`` — undefined tags are rejected.
    Each item has ``id``, ``name``, ``isSecurityTag``; an empty listing is
    normal. On failure returns ``{"mode": "error", "error": <slug>}``.
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
            description="Exact template name to filter to (case-sensitive on most builds).",
        ),
    ] = None,
    max_results: _DEF_MAX_RESULTS = None,
    skip: _DEF_SKIP = 0,
    *,
    summary_only: _DEF_SUMMARY_ONLY = False,
) -> dict[str, Any]:
    """List template definitions in the repository.

    Discover template names (each item: ``id``, ``name``, ``fieldCount``);
    pass ``template_name`` to filter to one. Does NOT enumerate a
    template's fields — use ``get_template_fields`` for that.
    On failure returns ``{"mode": "error", "error": <slug>}``.
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
                "Exact template name (case-sensitive on most builds); discover "
                "names with list_template_definitions."
            ),
        ),
    ],
    *,
    required_only: Annotated[
        bool,
        Field(
            default=False,
            description="Return only fields where is_required is true.",
        ),
    ] = False,
) -> dict[str, Any]:
    """Return one template's fields with full metadata, in a single call.

    Use before ``assign_template`` to construct its ``fields`` argument —
    replaces the list-templates + list-fields + cross-reference chain.

    Returns ``{"template_name", "template_id", "field_count", "fields"}``;
    each field has ``name``, ``field_type``, ``is_required``,
    ``is_multi_value``, ``list_values``, ``default_value``, ``constraint``.
    On failure returns ``{"mode": "error", "error": <slug>}`` —
    ``invalid_template_name`` includes the list of valid names.
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
        return local_error(
            "get_template_fields",
            "invalid_template_name",
            template_name=template_name,
            reason=(
                f"Template {template_name!r} is not defined in this "
                "repository. Match is case-sensitive."
            ),
            valid_template_names=sorted(template_defs.keys()),
        )
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
    """List the entry-link type definitions on this repository.

    Use before ``set_links`` — links need a ``linkTypeId`` from here. Each
    item has ``linkTypeId``, ``sourceLabel``, ``targetLabel`` (link types
    are directed). On failure returns ``{"mode": "error", "error": <slug>}``.
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
    """Return the audit-reason codes the authenticated user may supply.

    Use before an audited ``delete_entry`` (``LF_REQUIRE_AUDIT_REASON``).
    Response is grouped by operation type; pass the chosen ``id`` as
    ``audit_reason_id``. On failure returns ``{"mode": "error", "error":
    <slug>}``.
    """
    try:
        raw = await _app.get_client().get_audit_reasons()
    except LaserficheError as exc:
        return classify_lf_error("get_audit_reasons", exc)
    return raw
