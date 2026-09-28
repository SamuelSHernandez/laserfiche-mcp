"""FastMCP application instance, request lifespan, and shared accessors.

This module owns the singleton ``mcp`` object that every tool module
registers against. Putting it here (rather than inside ``server.py``)
lets the ``tools/*`` modules import ``mcp`` without creating a cycle
back to the CLI entrypoint in ``server.py``.

Public surface:
    mcp                          — the FastMCP instance
    get_settings()               — cached, env-driven Settings
    reset_settings_for_tests()   — clears the cache (monkeypatch helper)
    get_client()                 — per-request LaserficheClient
    clamp_max_results(requested) — apply LF_MAX_RESULTS_CEILING
    clamp_search_page_size(req)  — apply LF_MAX_PAGE_SIZE (SimpleSearches)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .auth import build_auth_strategy
from .client import LaserficheClient
from .config import Settings

logger = logging.getLogger("laserfiche_mcp")

_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached Settings, populating it from the environment on first call."""
    global _settings
    if _settings is None:
        # pydantic-settings populates every field from env vars / .env, so the
        # call site doesn't pass kwargs. Validation happens at model-load time.
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    """Reset the cached settings — for use only by tests via monkeypatch."""
    global _settings
    _settings = None


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Open one shared LaserficheClient for the server's lifetime."""
    settings = get_settings()
    if settings.http_oauth_destructive_scope and not settings.oauth_enabled:
        # Consistent with http_transport.py's loopback warning: loudly
        # flag a "configured X but it has no effect" misconfiguration
        # instead of the destructive-scope gate silently no-opping.
        logger.warning(
            "LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE is set but LF_HTTP_OAUTH_ISSUER "
            "is not — the destructive-scope gate has no effect until OAuth "
            "Resource Server mode is also configured. Under stdio or "
            "LF_HTTP_AUTH_TOKEN, LF_WRITE_TOOLS_ALLOWED is the only "
            "available fence."
        )
    auth = build_auth_strategy(settings)
    async with LaserficheClient(settings, auth) as client:
        yield {"client": client}


mcp = FastMCP(
    "laserfiche-mcp",
    # Tool names below are the v2 laserfiche_{resource}_{verb} names, which are
    # ALWAYS registered (see server._register_one) — unlike the legacy verb-
    # first names (search_content, get_entry, ...), which register only when
    # the operator opts in with LF_LEGACY_TOOL_NAMES=true. Referencing a
    # legacy-only name here would tell the model to call a tool that mostly
    # isn't registered.
    instructions=(
        "Tools for searching and reading documents in a Laserfiche repository. "
        "Use laserfiche_entry_search_content whenever the question is about "
        "what documents SAY — it returns the matched passages (page + excerpt) "
        "from the OCR index, usually answering without downloading anything. "
        "Use laserfiche_entry_search_natural to author a query when you need "
        "the server's templates and field names; laserfiche_folder_list for a "
        "known location; laserfiche_entry_get / laserfiche_field_values_get "
        "once you have an entry ID. To read more of a specific document, "
        "prefer laserfiche_document_get_edoc(mode='text', pages=...) — never "
        "mode='bytes' for anything large. Reading documents one by one to "
        "check the same fact across many of them is the single biggest way "
        "to exhaust context — each full-text read stays in the conversation "
        "and is never reclaimed; use laserfiche_entry_search_content there "
        "instead. When the user just wants to open or download a document "
        "themselves, hand them the `web_url` field from a search/get-entry "
        "result (present only when the operator has configured "
        "LF_WEB_CLIENT_URL_TEMPLATE) instead of reading the document through "
        "laserfiche_document_get_edoc — opening it still requires the user's "
        "own Laserfiche web-client login. "
        "Destructive write tools (laserfiche_entry_delete, "
        "laserfiche_document_edoc_delete, laserfiche_document_pages_delete, "
        "and similar) use a two-step preview-then-confirm contract: call once "
        "without confirmation_token to get back a preview plus a short-lived "
        "signed token, surface that preview to the user, then call again "
        "with the same arguments and confirmation_token to execute — a call "
        "with a different argument than the preview, or an expired/reused "
        "token, is rejected."
    ),
    lifespan=_lifespan,
)


def get_client() -> LaserficheClient:
    """Return the per-request LaserficheClient from the lifespan context."""
    ctx = mcp.get_context()
    client: LaserficheClient = ctx.request_context.lifespan_context["client"]
    return client


def clamp_max_results(requested: int | None) -> int:
    """Apply the configured ``max_results_default`` / ``max_results_ceiling`` policy."""
    settings = get_settings()
    value = settings.max_results_default if requested is None else requested
    return min(max(1, value), settings.max_results_ceiling)


def clamp_search_page_size(requested: int | None) -> int:
    """Hard cap on search_natural pagination, separate from list/folder cap.

    Some self-hosted SimpleSearches implementations 400 on $top values above
    a server-internal limit, so this cap defaults lower (LF_MAX_PAGE_SIZE).
    """
    settings = get_settings()
    value = settings.max_results_default if requested is None else requested
    return min(max(1, value), settings.max_page_size)
