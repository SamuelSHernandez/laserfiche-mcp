"""``search_natural`` — two-mode search with guidance + automatic query repair."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from .. import _app
from .._app import clamp_search_page_size
from ..client import LaserficheClient
from ..errors import LaserficheError
from ..models import (
    SearchAttempt,
    SearchNaturalResponse,
    SearchResults,
    TemplateHint,
)
from ..search import (
    LF_GRAMMAR_REFERENCE,
    build_candidate_queries,
    repair_escape_quotes,
    repair_wildcard_name,
)
from ._registry import register


async def _sample_folder_templates(
    client: LaserficheClient,
    folder_path: str | None,
) -> tuple[list[TemplateHint], list[str]]:
    """Sample up to ~10 entries from ``folder_path`` and collect template hints.

    Returns ``(templates, notes)``. ``notes`` carries any caveats (folder
    missing, folder empty, individual fetch failures) that the host LLM
    should see in the guidance response.
    """
    notes: list[str] = []

    folder_id: int = 1  # Repository root
    if folder_path:
        try:
            folder = await client.get_entry_by_path(folder_path)
        except LaserficheError as exc:
            notes.append(
                f"Could not resolve folder_path {folder_path!r}: {exc}. "
                "Sampled from the repository root instead."
            )
        else:
            resolved = folder.get("id")
            if resolved and resolved > 0:
                folder_id = resolved
            else:
                notes.append(
                    f"folder_path {folder_path!r} resolved to an empty entry; "
                    "sampled from the repository root instead."
                )

    try:
        children = await client.list_folder(folder_id, max_results=10)
    except LaserficheError as exc:
        notes.append(f"Could not list folder {folder_id}: {exc}")
        return [], notes

    entries = children.get("value") or []
    if not entries:
        notes.append(f"Folder {folder_id} had no children to sample.")
        return [], notes

    entry_ids: list[int] = [e["id"] for e in entries if e.get("id")]
    detail_results = await asyncio.gather(
        *[client.get_entry(eid) for eid in entry_ids],
        return_exceptions=True,
    )

    # Map template_name → a representative entry_id we can fetch fields from.
    template_sample: dict[str, int] = {}
    for entry_id, detail in zip(entry_ids, detail_results, strict=False):
        if isinstance(detail, BaseException):
            continue
        name = detail.get("templateName") or detail.get("TemplateName")
        if name:
            template_sample.setdefault(name, entry_id)

    if not template_sample:
        notes.append(
            "Sampled entries had no template assigned, so no template "
            "hints could be derived. Field-search queries will need to "
            "be authored without auto-discovery."
        )
        return [], notes

    field_results = await asyncio.gather(
        *[client.get_field_values(eid) for eid in template_sample.values()],
        return_exceptions=True,
    )

    templates: list[TemplateHint] = []
    for template_name, fr in zip(template_sample.keys(), field_results, strict=False):
        if isinstance(fr, BaseException):
            templates.append(TemplateHint(template_name=template_name, field_names=[]))
            continue
        raw_fields = fr.get("value") or fr.get("Value") or []
        field_names: list[str] = []
        for f in raw_fields:
            fname = f.get("fieldName") or f.get("FieldName")
            if fname and fname not in field_names:
                field_names.append(fname)
        templates.append(TemplateHint(template_name=template_name, field_names=field_names))

    return templates, notes


def _follow_up_hint(folder_path: str | None, max_results: int) -> str:
    folder_arg = f', folder_path="{folder_path}"' if folder_path else ""
    return (
        "Pick one of the candidate_queries (or refine it) and call "
        f'search_natural(question=..., lf_query="<query>"{folder_arg}, '
        f"max_results={max_results})."
    )


def _dump(**kwargs: Any) -> dict[str, Any]:
    """Build a ``SearchNaturalResponse`` and dump it to a dict."""
    return SearchNaturalResponse(**kwargs).model_dump()


async def _run_guidance_mode(
    *,
    client: LaserficheClient,
    question: str,
    folder_path: str | None,
    max_results: int,
    effective_max: int,
) -> dict[str, Any]:
    """Mode A — discover templates and propose candidate queries."""
    templates, notes = await _sample_folder_templates(client, folder_path)
    if effective_max != max_results:
        notes.append(
            f"max_results was clamped from {max_results} to {effective_max} by LF_MAX_PAGE_SIZE."
        )
    candidates = build_candidate_queries(question, folder_path, templates)
    return _dump(
        mode="guidance",
        question=question,
        folder_path=folder_path,
        grammar=LF_GRAMMAR_REFERENCE,
        discovered_templates=templates,
        candidate_queries=candidates,
        follow_up=_follow_up_hint(folder_path, effective_max),
        notes=notes,
        effective_max_results=effective_max,
    )


def _next_repair(
    current_query: str,
    repairs_applied: list[str],
    fuzzy: bool,
) -> tuple[str, str] | None:
    """Pick the next repair to try; return ``(repaired_query, repair_name)`` or None."""
    if "escape_quotes" not in repairs_applied:
        repaired = repair_escape_quotes(current_query)
        if repaired is not None:
            return repaired, "escape_quotes"

    if fuzzy and "wildcard_wrap" not in repairs_applied:
        repaired = repair_wildcard_name(current_query)
        if repaired is not None:
            return repaired, "wildcard_wrap"

    return None


async def _run_execute_mode(
    *,
    client: LaserficheClient,
    question: str,
    lf_query: str,
    fuzzy: bool,
    effective_max: int,
) -> dict[str, Any]:
    """Mode B — execute the query, retrying with automatic repairs on HTTP 400."""
    attempts: list[SearchAttempt] = []
    repairs_applied: list[str] = []
    current_query = lf_query
    current_repair: str | None = None

    # At most three calls: original + escape_quotes + wildcard_wrap.
    for _ in range(3):
        try:
            raw = await client.search_entries(current_query, max_results=effective_max)
        except LaserficheError as exc:
            attempts.append(
                SearchAttempt(
                    query=current_query,
                    repair=current_repair,
                    status_code=exc.status_code,
                    error_body=str(exc),
                )
            )
            if exc.status_code != 400:
                # Non-400 errors are not in the repair contract — surface immediately.
                return _dump(
                    mode="error",
                    question=question,
                    lf_query=lf_query,
                    attempts=attempts,
                    final_error=str(exc),
                    next_action=(
                        "Server returned a non-400 error. Check repository "
                        "permissions, network reachability, and credentials "
                        "before retrying."
                    ),
                )

            repair = _next_repair(current_query, repairs_applied, fuzzy)
            if repair is None:
                return _dump(
                    mode="error",
                    question=question,
                    lf_query=lf_query,
                    attempts=attempts,
                    final_error=str(exc),
                    next_action=(
                        "All automatic repairs are exhausted. Read the grammar "
                        "(call search_natural without lf_query) and author a "
                        "new query — common fixes: quote string values, use "
                        "wildcards in Name= clauses, scope with "
                        '{LF:LookIn="\\\\path"}, or switch to a template '
                        'field clause like {[Template]:[Field]="value"}.'
                    ),
                )
            current_query, current_repair = repair
            repairs_applied.append(current_repair)
            continue

        # Success path.
        results = SearchResults.from_api(raw)
        pagination_unknown = results.next_link is None and len(results.entries) >= effective_max
        return _dump(
            mode="results",
            question=question,
            lf_query=current_query,
            repairs_applied=repairs_applied,
            entries=results.entries,
            total_count=results.total_count,
            next_link=results.next_link,
            pagination_unknown=pagination_unknown,
            effective_max_results=effective_max,
        )

    # Loop exhausted without returning — should not happen, but be safe.
    return _dump(
        mode="error",
        question=question,
        lf_query=lf_query,
        attempts=attempts,
        final_error="search_natural exhausted repair attempts without a definitive outcome.",
        next_action="Retry with a refined lf_query.",
    )


@register(v2_name="laserfiche_entry_search_natural")
async def search_natural(
    question: Annotated[
        str,
        Field(
            description=(
                "The user's natural-language search question. Used by Mode A "
                "to extract keywords for candidate queries and surfaced in "
                "Mode B responses for correlation."
            ),
            examples=[
                "find the latest invoice from Acme",
                "what onboarding docs do we have for Smith?",
            ],
        ),
    ],
    lf_query: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Laserfiche query to execute (Mode B). Omit to get guidance "
                "(Mode A): grammar reference, sampled templates, candidate "
                "queries to refine."
            ),
            examples=['{LF:Name="*Acme*"}', '{[Invoice]:[Vendor]="Acme*"}'],
        ),
    ] = None,
    folder_path: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Backslash-delimited folder path. In Mode A, narrows the "
                "template sample to this subtree; in Mode B, the LLM should "
                "embed {LF:LookIn=\"<path>\"} in lf_query itself if scoping "
                "is wanted."
            ),
            examples=["\\HR\\Personnel", "\\Imports\\2024"],
        ),
    ] = None,
    max_results: Annotated[
        int,
        Field(
            default=50,
            description=(
                "Page size. Clamped to LF_MAX_PAGE_SIZE (default 100) — some "
                "self-hosted SimpleSearches implementations 400 on larger $top."
            ),
            ge=1,
            le=500,
        ),
    ] = 50,
    *,
    fuzzy: Annotated[
        bool,
        Field(
            default=True,
            description=(
                "When True (default), Mode B attempts a wildcard-wrap repair "
                "if the server 400s on a Name=value clause with no wildcards. "
                "Set False for exact-match queries that should NOT be relaxed."
            ),
        ),
    ] = True,
) -> dict[str, Any]:
    """Two-mode search: guidance first, then execution with automatic repair.

    Most Laserfiche servers reject malformed query syntax with a generic HTTP
    400. This tool gives the host LLM a structured way to author a working
    query without trial-and-error against the user.

    **Mode A — ``lf_query`` omitted**
        Returns ``mode="guidance"`` with:
          * ``grammar`` — the Laserfiche search syntax reference this server
            understands, with examples.
          * ``discovered_templates`` — template names and field names sampled
            from ``folder_path`` (or the repository root). Use these to
            author template-field queries like
            ``{[Personnel]:[Last Name]="Smith"}``.
          * ``candidate_queries`` — up to 3 starter queries built from the
            question's keywords. Pick one or refine it, then call again with
            ``lf_query``.
          * ``follow_up`` — the exact follow-up call shape.

    **Mode B — ``lf_query`` provided**
        Executes the query and returns ``mode="results"`` (or
        ``mode="error"`` with structured detail). On HTTP 400, up to two
        automatic repairs are attempted:

          1. Escape unescaped ``"`` characters inside ``="..."`` value spans.
          2. Wrap ``Name="value"`` values in ``*`` wildcards (only when
             ``fuzzy=True`` and the value has no wildcard).

        Each attempt is recorded in ``attempts`` on the error response.

    **Pagination**
        ``max_results`` is clamped to ``LF_MAX_PAGE_SIZE`` (default 100).
        Some self-hosted SimpleSearches implementations 400 on larger
        ``$top`` values, so the cap is lower than the list-folder ceiling.
        When ``next_link`` is null but the result count hit the effective
        cap, ``pagination_unknown=true`` is surfaced — there may be more
        results, the server just didn't say.

    **What this tool does NOT do**
        It does not silently fall back to folder traversal. If both repairs
        still 400, you get a structured error so the user knows search failed
        and the host LLM can author a fresh query.

    **On failure**
        Mode B returns ``{mode: "error", attempts: [...]}`` with the full
        repair history visible — each attempt records the query, the repair
        tag applied, the HTTP status, and the server's error body, enough
        context for the LLM to write a different query. Other failures
        (auth, rate limit, network) come back via the generic error
        contract; see docs/error-contract.md.
    """
    effective_max = clamp_search_page_size(max_results)
    client = _app.get_client()

    if lf_query is None:
        return await _run_guidance_mode(
            client=client,
            question=question,
            folder_path=folder_path,
            max_results=max_results,
            effective_max=effective_max,
        )

    return await _run_execute_mode(
        client=client,
        question=question,
        lf_query=lf_query,
        fuzzy=fuzzy,
        effective_max=effective_max,
    )
