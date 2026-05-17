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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .auth import build_auth_strategy
from .client import LaserficheClient
from .config import Settings

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
    auth = build_auth_strategy(settings)
    async with LaserficheClient(settings, auth) as client:
        yield {"client": client}


mcp = FastMCP(
    "laserfiche-mcp",
    instructions=(
        "Tools for searching and reading documents in a Laserfiche repository. "
        "Use search_entries when the user describes what they're looking for in "
        "natural language; use list_folder when they reference a known location; "
        "use get_entry or get_field_values once you have an entry ID. "
        "Most workflows are: (1) locate an entry via search/path/folder, "
        "(2) call get_entry for metadata or get_field_values for template data, "
        "(3) optionally call get_document_text for the document body."
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
