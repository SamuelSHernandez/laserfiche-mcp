"""Asynchronous ``/Searches`` flow — the only path that yields context hits.

``POST /SimpleSearches`` (see ``_EntriesMixin.search_entries``) runs
synchronously and answers *which* entries matched. It cannot answer *where*
in each document the match landed, because the per-hit page numbers and
surrounding text excerpts are keyed by a search token that only the
asynchronous flow produces.

The flow is four steps on both API versions, with different spellings:

v1::

    POST   Searches                              {"searchCommand": "..."}  -> {"token"}
    GET    Searches/{token}                      -> {"status", "percentComplete", ...}
    GET    Searches/{token}/Results              -> {"value": [{..., "rowNumber"}]}
    GET    Searches/{token}/Results/{row}/ContextHits
    DELETE Searches/{token}

v2::

    POST   Searches/SearchAsync                  {"searchCommand": "..."}  -> {"taskId"}
    GET    Tasks?taskIds={taskId}                -> {"value": [{"status", ...}]}
    GET    Searches/{taskId}/Results             -> {"value": [{..., "rowNumber"}]}
    GET    Searches/{taskId}/Results/{row}/ContextHits
    DELETE Tasks?taskIds={taskId}

**Search tokens are a scarce, session-scoped resource** — Laserfiche caps the
number of concurrently active searches per user session (two, on v1). Every
caller must close its token, which is why the ``search_content`` tool wraps
this whole flow in a ``try/finally`` rather than handing a live token to the
model.
"""

from __future__ import annotations

from typing import Any

from ..config import ApiVersion
from ..errors import LaserficheError
from ._core import _CoreClient

# Terminal task states. The API spells these in PascalCase; we compare
# case-insensitively because self-hosted builds have not been consistent.
_TERMINAL_OK = {"completed"}
_TERMINAL_BAD = {"failed", "canceled", "cancelled"}


class _SearchMixin(_CoreClient):
    """The asynchronous search flow: create, poll, read results, close."""

    async def create_search(self, query: str) -> str:
        """Start an async search. Returns the search token.

        The token identifies a server-side result set that stays cached for a
        limited time and counts against the session's active-search budget.
        Pair every call with :meth:`close_search`.
        """
        suffix = "Searches" if self._api_version is ApiVersion.V1 else "Searches/SearchAsync"
        raw = await self._request_json(
            "POST",
            self._repo_path(suffix),
            json={"searchCommand": query},
        )
        # v1 answers {"token": ...}; v2 answers {"taskId": ...}. Some builds
        # PascalCase both. Accept whichever is present rather than branching on
        # api_version, so a server that answers in the other dialect still works.
        for key in ("token", "Token", "taskId", "TaskId", "operationToken", "OperationToken"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
        raise LaserficheError(
            f"Search was accepted but the response carried no search token: {raw!r}"
        )

    async def get_search_status(self, token: str) -> dict[str, Any]:
        """Poll a running search. Returns a normalized status dict.

        Shape: ``{"status": <str>, "percent_complete": <int|None>,
        "errors": [...], "done": <bool>, "failed": <bool>}``.

        ``status`` is passed through verbatim from the server (typically
        ``NotStarted`` / ``InProgress`` / ``Completed`` / ``Failed`` /
        ``Canceled``); ``done`` and ``failed`` are the case-insensitive
        interpretation callers should branch on.
        """
        if self._api_version is ApiVersion.V1:
            raw = await self._request_json("GET", self._repo_path(f"Searches/{token}"))
            payload = raw
        else:
            raw = await self._request_json(
                "GET",
                self._repo_path("Tasks"),
                params={"taskIds": token},
            )
            values = raw.get("value") or raw.get("Value") or []
            payload = values[0] if isinstance(values, list) and values else {}

        status = payload.get("status") or payload.get("Status") or ""
        status_lower = str(status).lower()
        percent = payload.get("percentComplete", payload.get("PercentComplete"))
        errors = payload.get("errors") or payload.get("Errors") or []
        return {
            "status": status,
            "percent_complete": percent if isinstance(percent, int) else None,
            "errors": errors,
            "done": status_lower in _TERMINAL_OK,
            "failed": status_lower in _TERMINAL_BAD,
        }

    async def get_search_results(
        self,
        token: str,
        *,
        max_results: int = 25,
        skip: int = 0,
    ) -> dict[str, Any]:
        """Read one page of a completed search's results.

        Each row carries a ``rowNumber`` — the 1-based index
        :meth:`get_search_context_hits` needs. Row numbers are positions in
        the *whole* result set, so they stay valid across pages.

        v1 rejects OData paging parameters on some self-hosted builds (the
        same ``errorCode 216`` that bites ``SimpleSearches``), so we page
        client-side there and let v2 page server-side.
        """
        suffix = f"Searches/{token}/Results"
        if self._api_version is ApiVersion.V1:
            raw = await self._request_json("GET", self._repo_path(suffix))
            value = raw.get("value")
            if isinstance(value, list):
                raw = {**raw, "value": value[skip : skip + max_results]}
            return raw

        params: dict[str, Any] = {"$top": max_results, "$skip": skip, "$count": "true"}
        return await self._request_json("GET", self._repo_path(suffix), params=params)

    async def get_search_context_hits(
        self,
        token: str,
        row_number: int,
    ) -> dict[str, Any]:
        """Fetch the matched passages for one result row.

        ``row_number`` is the 1-based ``rowNumber`` from
        :meth:`get_search_results` — not an entry ID.

        Returns the raw ``{"value": [SearchContextHit, ...]}`` payload. Each
        hit carries ``pageNumber``, ``context`` (the surrounding text) and
        ``highlight1Offset`` / ``highlight1Length`` locating the match within
        that context.
        """
        return await self._request_json(
            "GET",
            self._repo_path(f"Searches/{token}/Results/{row_number}/ContextHits"),
        )

    async def close_search(self, token: str) -> None:
        """Release a search token and its cached result set.

        Best-effort: exceptions are the caller's to handle, but callers
        should generally swallow them — a failed close is not worth
        surfacing over a successful search, and the token expires on its
        own eventually.
        """
        if self._api_version is ApiVersion.V1:
            await self._request_json("DELETE", self._repo_path(f"Searches/{token}"))
        else:
            await self._request_json(
                "DELETE",
                self._repo_path("Tasks"),
                params={"taskIds": token},
            )
