"""``search_content`` — full-text search that returns the matched passages.

This is the MCP wrapper. The create → poll → read → close orchestration lives
in ``ops/content_search.py`` so the ``laserfiche-mcp search`` subcommand runs
exactly the same flow without an LLM in the loop.

``search_entries`` (SimpleSearches) can already query the full-text index, but
it only answers *which* entries matched. Only the async flow exposes
``ContextHits``: the page number and surrounding text for every match,
straight out of Laserfiche's own index — which is where OCR output lives.

The practical consequence is that "what does this contract say about
termination?" is answerable for a few hundred tokens across a dozen documents,
instead of downloading each one and extracting its text.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .. import _app
from .._app import clamp_search_page_size, get_settings
from ..errors import LaserficheError, classify_lf_error
from ..observability import get_request_id_or_new
from ..ops.content_search import STATUS_ABSENT, build_search_command, run_search
from ._registry import register

__all__ = ["build_search_command", "search_content"]


def _classify_create_error(exc: LaserficheError, command: str) -> dict[str, Any]:
    """Classify a failure to start the search.

    A 404/405/501 here means the build predates the async search endpoints
    entirely — a distinct condition from "nothing matched", and one the model
    should recover from by falling back to ``search_entries`` rather than
    retrying.
    """
    payload = classify_lf_error("search_content", exc, extra={"query": command})
    if exc.status_code in STATUS_ABSENT:
        payload["error"] = "async_search_unavailable"
        payload["hint"] = (
            "This Laserfiche build does not expose the asynchronous /Searches "
            "endpoints, so context hits are unavailable. Fall back to "
            "search_entries with the same query — it searches the same "
            "full-text index and returns which entries matched, just without "
            "the page numbers and excerpts."
        )
    return payload


@register(v2_name="laserfiche_entry_search_content")
async def search_content(
    query: Annotated[
        str,
        Field(
            description=(
                "A phrase to find inside documents, or a full Laserfiche search "
                "expression if it starts with '{'. A bare phrase is wrapped into "
                '{LF:Basic~="..."} for you.'
            ),
            examples=[
                "unpaid balance",
                "termination for convenience",
                '{LF:Basic~="asbestos",option="D"}',
            ],
            min_length=1,
        ),
    ],
    folder_path: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Restrict the search to this folder subtree. Passed through "
                "verbatim as a LookIn clause."
            ),
            examples=["\\\\HR\\\\Personnel", "\\\\Imports\\\\2024"],
        ),
    ] = None,
    max_results: Annotated[
        int | None,
        Field(
            default=None,
            description="How many matching entries to return. Defaults to "
            "LF_MAX_RESULTS_DEFAULT (25), capped by LF_MAX_PAGE_SIZE.",
            ge=1,
            le=1000,
        ),
    ] = None,
    hits_for_top: Annotated[
        int,
        Field(
            default=5,
            description=(
                "Fetch matched passages for this many top results (one extra "
                "request each). 0 = matches only, no excerpts."
            ),
            ge=0,
            le=50,
        ),
    ] = 5,
    hits_per_entry: Annotated[
        int,
        Field(
            default=3,
            description="Max passages per entry (capped by LF_SEARCH_CONTEXT_HITS_MAX).",
            ge=1,
            le=100,
        ),
    ] = 3,
    context_chars: Annotated[
        int,
        Field(
            default=300,
            description="Approximate passage size in characters, centered on the match.",
            ge=40,
            le=4000,
        ),
    ] = 300,
    skip: Annotated[
        int,
        Field(
            default=0,
            description="Skip this many results (the search re-runs server-side per call).",
            ge=0,
        ),
    ] = 0,
    timeout_seconds: Annotated[
        float | None,
        Field(
            default=None,
            description="Give up after this long. Defaults to LF_SEARCH_TIMEOUT_SECONDS (60).",
            gt=0,
            le=600,
        ),
    ] = None,
) -> dict[str, Any]:
    """Search document text and return the passages that matched.

    **Use this whenever the question is about what documents *say*.** Returns
    the matched text itself — page number plus surrounding excerpt — from
    Laserfiche's full-text index, which is where OCR output for scanned
    documents lives. Usually answers without downloading anything; when an
    excerpt shows the right document, follow up with
    ``get_document_edoc(mode="text", pages=...)`` for more.

    A bare ``query`` becomes ``{LF:Basic~="<phrase>"}`` (document text,
    fields, annotations, names). Pass raw syntax starting with ``{`` for
    control, e.g. ``option="D"`` (document text only).

    Sibling tools: ``search_entries`` = raw query, no excerpts;
    ``search_by_name`` = filename patterns.

    Returns ``{"mode": "content_search", "total_count", "results": [...]}``;
    each result has ``entry_id``, ``name``, ``hit_count`` and ``hits``
    (``{page, text, match}``) for the top ``hits_for_top`` results. On
    failure returns ``{"mode": "error", "error": <slug>}`` —
    ``async_search_unavailable`` (no /Searches on this build: fall back to
    ``search_entries``), ``search_timeout`` (narrow with ``folder_path`` or
    raise ``timeout_seconds``), ``search_failed`` (see ``server_errors``).
    """
    settings = get_settings()
    command = build_search_command(query, folder_path)
    page_size = clamp_search_page_size(max_results)
    hits_cap = min(hits_per_entry, settings.search_context_hits_max)
    timeout = timeout_seconds if timeout_seconds is not None else settings.search_timeout_seconds

    try:
        outcome = await run_search(
            _app.get_client(),
            command,
            page_size=page_size,
            skip=skip,
            hits_for_top=hits_for_top,
            hits_per_entry=hits_cap,
            context_chars=context_chars,
            timeout_seconds=timeout,
            poll_interval=settings.search_poll_interval_seconds,
        )
    except LaserficheError as exc:
        return _classify_create_error(exc, command)

    if outcome.failure is not None:
        return {
            "mode": "error",
            "operation": "search_content",
            "query": command,
            "request_id": get_request_id_or_new(),
            **outcome.failure,
        }

    return {
        "mode": "content_search",
        "query": command,
        "total_count": outcome.total_count,
        "returned": len(outcome.results),
        "hits_fetched_for": outcome.hits_fetched_for,
        "results": [r.model_dump() for r in outcome.results],
    }
