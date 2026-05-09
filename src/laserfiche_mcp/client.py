"""Thin async client for the Laserfiche Repository API.

Deliberately a thin pass-through over httpx — all the Laserfiche-specific
shaping happens here, so ``server.py`` can stay focused on tool definitions
and response framing.

Endpoint paths follow the self-hosted Repository API Server v1/v2
conventions. Cloud will get a parallel client in v2.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from typing import Any
from urllib.parse import urljoin

import httpx

from .auth import AuthStrategy
from .config import Settings

logger = logging.getLogger("laserfiche_mcp.client")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LaserficheError(Exception):
    """Raised when the Repository API returns an error or unexpected response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def build_repo_path(base_url: str, repository_id: str, suffix: str) -> str:
    """Construct a /v2/Repositories/{repo}/{suffix} URL.

    Pulled out of ``LaserficheClient`` so it's directly unit-testable —
    URL composition is one of the easier places to introduce subtle bugs.
    """
    if not base_url.endswith("/"):
        base_url += "/"
    suffix = suffix.lstrip("/")
    return urljoin(base_url, f"v2/Repositories/{repository_id}/{suffix}")


class LaserficheClient:
    """Async client for the self-hosted Repository API Server."""

    def __init__(self, settings: Settings, auth: AuthStrategy) -> None:
        self._settings = settings
        self._auth = auth
        self._base_url = str(settings.repo_api_url) if settings.repo_api_url else ""
        self._repository_id = settings.repository_id or ""
        self._http: httpx.AsyncClient | None = None

        if not settings.verify_ssl:
            warnings.warn(
                "TLS certificate verification is DISABLED (LF_VERIFY_SSL=false). "
                "This is insecure outside of trusted internal dev environments.",
                stacklevel=2,
            )

    async def __aenter__(self) -> LaserficheClient:
        self._http = httpx.AsyncClient(
            timeout=self._settings.request_timeout_seconds,
            verify=self._settings.verify_ssl,
            headers={"Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- Internals ---------------------------------------------------------

    def _repo_path(self, suffix: str) -> str:
        return build_repo_path(self._base_url, self._repository_id, suffix)

    async def _send(self, request: httpx.Request) -> httpx.Response:
        """Apply auth, send, and retry on transient failures."""
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        attempts = max(1, self._settings.retry_attempts + 1)
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                await self._auth.apply(request)
                response = await self._http.send(request)
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    break
                delay = 2 ** attempt
                logger.warning(
                    "Network error on %s %s (attempt %d/%d): %s; retrying in %ds",
                    request.method, request.url, attempt + 1, attempts, exc, delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                delay = 2 ** attempt
                logger.warning(
                    "Retryable status %d on %s %s (attempt %d/%d); retrying in %ds",
                    response.status_code, request.method, request.url,
                    attempt + 1, attempts, delay,
                )
                await asyncio.sleep(delay)
                continue

            return response

        raise LaserficheError(
            f"Network error after {attempts} attempt(s): {last_exc}",
        ) from last_exc

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        request = self._http.build_request(method, url, params=params, json=json)
        response = await self._send(request)

        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise LaserficheError(
                f"Laserfiche API error {response.status_code}: {detail}",
                status_code=response.status_code,
            )

        if not response.content:
            return {}
        return response.json()

    # --- Read operations (v1) ---------------------------------------------

    async def get_entry(self, entry_id: int) -> dict[str, Any]:
        return await self._request("GET", self._repo_path(f"Entries/{entry_id}"))

    async def get_entry_by_path(self, full_path: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            self._repo_path("Entries/ByPath"),
            params={"fullPath": full_path},
        )

    async def list_folder(
        self,
        folder_id: int,
        *,
        max_results: int = 25,
        skip: int = 0,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            self._repo_path(f"Entries/{folder_id}/Children"),
            params={"$top": max_results, "$skip": skip},
        )

    async def search_entries(
        self,
        query: str,
        *,
        max_results: int = 25,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            self._repo_path("Entries/SearchEntries"),
            params={"searchCommand": query, "$top": max_results},
        )

    async def get_field_values(self, entry_id: int) -> dict[str, Any]:
        return await self._request(
            "GET",
            self._repo_path(f"Entries/{entry_id}/Fields"),
        )

    async def get_entry_content(self, entry_id: int) -> bytes:
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")
        url = self._repo_path(f"Entries/{entry_id}/Edoc")
        request = self._http.build_request("GET", url)
        response = await self._send(request)
        if response.status_code >= 400:
            raise LaserficheError(
                f"Failed to download entry {entry_id}: HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return response.content
