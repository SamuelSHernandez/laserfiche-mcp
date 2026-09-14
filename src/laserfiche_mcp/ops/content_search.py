"""Orchestration for the asynchronous ``/Searches`` flow.

Lives here rather than in ``tools/`` because both callers need it and only
one of them is an LLM: the MCP tool wraps this into a response dict, and the
``laserfiche-mcp search`` subcommand prints it. Keeping the create → poll →
read → close cycle in one place means the CLI can never drift from the tool,
and neither can forget to release a search token.

The token discipline is the reason this is a single function rather than a
handle the caller drives: Laserfiche caps concurrently active searches per
session (two, on v1), so a leaked token is a denial of service against the
user's own account.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import LaserficheError
from ..models import ContentSearchResult, ContextHit

# Ceiling on a single status poll's delay, regardless of how long the search
# runs. Starts at the configured interval and backs off to this.
_MAX_POLL_INTERVAL = 2.0

# How many ContextHits requests may be in flight at once. Each result row is a
# separate round-trip, so an unbounded gather over 25 rows would hammer a
# self-hosted server that is already busy running the search.
_HITS_CONCURRENCY = 5

# A status endpoint answering one of these doesn't exist on this build. Stop
# polling and try the results endpoint directly rather than failing.
STATUS_ABSENT = {404, 405, 501}


def build_search_command(query: str, folder_path: str | None) -> str:
    """Turn a caller's ``query`` into a Laserfiche search command.

    A query that already starts with ``{`` is passed through as raw search
    syntax. Anything else is treated as a phrase and wrapped into a content
    clause, so the common case — the caller has a phrase, not a grammar —
    needs no syntax knowledge at all.

    ``folder_path`` is appended verbatim as a ``LookIn`` clause, matching how
    ``search_natural`` builds its candidates.
    """
    stripped = query.strip()
    if stripped.startswith("{"):
        command = stripped
    else:
        # Only `"` needs escaping inside a value span; backslashes are literal
        # (Laserfiche paths are full of them).
        escaped = stripped.replace('"', '\\"')
        command = f'{{LF:Basic~="{escaped}"}}'

    if folder_path:
        command = f'{command} & {{LF:LookIn="{folder_path}"}}'
    return command


@dataclass
class SearchOutcome:
    """What a completed (or abandoned) search produced."""

    results: list[ContentSearchResult] = field(default_factory=list)
    total_count: int | None = None
    hits_fetched_for: int = 0
    failure: dict[str, Any] | None = None
    """Set when the search timed out or the server aborted it. The results
    list is empty in that case; the dict carries the error slug and detail."""


async def await_search(
    client: Any,
    token: str,
    *,
    timeout_seconds: float,
    poll_interval: float,
) -> dict[str, Any] | None:
    """Poll until the search finishes. Returns an error dict, or None on success.

    A status endpoint that answers 404/405/501 is treated as "this build
    doesn't expose search status" rather than a failure — polling stops and
    the caller tries the results endpoint directly.
    """
    deadline = time.monotonic() + timeout_seconds
    interval = poll_interval
    last_percent: int | None = None

    while True:
        try:
            status = await client.get_search_status(token)
        except LaserficheError as exc:
            if exc.status_code in STATUS_ABSENT:
                return None
            raise

        if status["done"]:
            return None
        if status["failed"]:
            return {
                "kind": "upstream_unavailable",
                "error": "search_failed",
                "status": status["status"],
                "server_errors": status["errors"],
            }

        last_percent = status["percent_complete"]
        if time.monotonic() >= deadline:
            return {
                "kind": "upstream_unavailable",
                "error": "search_timeout",
                "status": status["status"],
                "percent_complete": last_percent,
                "timeout_seconds": timeout_seconds,
                "message": (
                    f"Search was still running after {timeout_seconds:g}s and was "
                    "abandoned. Narrow it with folder_path, or raise "
                    "timeout_seconds / LF_SEARCH_TIMEOUT_SECONDS."
                ),
            }

        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        interval = min(interval * 1.5, _MAX_POLL_INTERVAL)


async def attach_context_hits(
    client: Any,
    token: str,
    results: list[ContentSearchResult],
    *,
    hits_per_entry: int,
    context_chars: int,
) -> None:
    """Fetch and attach matched passages for each result, in place.

    One request per row, bounded by ``_HITS_CONCURRENCY``. A row whose hits
    fail to load keeps its ``hits_error`` and still appears in the response —
    a partial answer beats dropping a genuine match on the floor.
    """
    semaphore = asyncio.Semaphore(_HITS_CONCURRENCY)

    async def fetch(result: ContentSearchResult) -> None:
        if result.row_number is None:
            result.hits_error = "no_row_number"
            return
        async with semaphore:
            try:
                raw = await client.get_search_context_hits(token, result.row_number)
            except LaserficheError as exc:
                result.hits_error = f"{exc.status_code or 'error'}"
                return

        items = raw.get("value") or raw.get("Value") or []
        if not isinstance(items, list):
            items = []
        result.hit_count = len(items)
        result.hits_truncated = len(items) > hits_per_entry
        result.hits = [
            ContextHit.from_api(item, context_chars=context_chars)
            for item in items[:hits_per_entry]
        ]

    await asyncio.gather(*(fetch(r) for r in results))


async def run_search(
    client: Any,
    command: str,
    *,
    page_size: int,
    skip: int = 0,
    hits_for_top: int = 5,
    hits_per_entry: int = 3,
    context_chars: int = 300,
    timeout_seconds: float = 60.0,
    poll_interval: float = 1.0,
) -> SearchOutcome:
    """Run one async search end to end and release the token.

    ``command`` must already be a Laserfiche search command — build it with
    :func:`build_search_command`.

    Raises ``LaserficheError`` for transport and API failures (including a
    missing ``/Searches`` endpoint, which surfaces as 404/405/501 from
    ``create_search``); the caller decides how to present those. Timeouts and
    server-side search failures come back as ``outcome.failure`` instead,
    because they are outcomes of a search that did start.
    """
    token = await client.create_search(command)

    try:
        failure = await await_search(
            client,
            token,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )
        if failure is not None:
            return SearchOutcome(failure=failure)

        raw = await client.get_search_results(token, max_results=page_size, skip=skip)
        items = raw.get("value") or raw.get("Value") or []
        results = [ContentSearchResult.from_api(item) for item in items if isinstance(item, dict)]

        if hits_for_top > 0 and results:
            await attach_context_hits(
                client,
                token,
                results[:hits_for_top],
                hits_per_entry=hits_per_entry,
                context_chars=context_chars,
            )
    finally:
        # The session's active-search budget is small; never leak a token.
        # A failed close is not worth surfacing over a successful search —
        # the token expires server-side on its own.
        with contextlib.suppress(LaserficheError):
            await client.close_search(token)

    return SearchOutcome(
        results=results,
        total_count=raw.get("@odata.count"),
        hits_fetched_for=min(hits_for_top, len(results)),
    )
