"""Thin async client for the Laserfiche Repository API.

This module is deliberately a thin pass-through over httpx — all the
Laserfiche-specific shaping happens here, so ``server.py`` can stay focused
on tool definitions and response framing.

Endpoint paths follow the self-hosted Repository API Server v1/v2 conventions.
If you're targeting cloud, base_url and some path shapes will differ; we'll
add a ``CloudClient`` subclass in v2.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

from .auth import AuthStrategy
from .config import Settings


class LaserficheError(Exception):
    """Raised when the Repository API returns an error or unexpected response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LaserficheClient:
    """Async client for the self-hosted Repository API Server."""

    def __init__(self, settings: Settings, auth: AuthStrategy) -> None:
        self._settings = settings
        self._auth = auth
        base = settings.repo_api_url or ""
        if not base.endswith("/"):
            base += "/"
        self._base_url = base
        self._repository_id = settings.repository_id or ""
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> LaserficheClient:
        self._http = httpx.AsyncClient(
            timeout=self._settings.request_timeout_seconds,
            headers={"Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    # --- Internals ---------------------------------------------------------

    def _repo_path(self, suffix: str) -> str:
        """Build path under /v2/Repositories/{repo}/..."""
        if suffix.startswith("/"):
            suffix = suffix[1:]
        return urljoin(self._base_url, f"v2/Repositories/{self._repository_id}/{suffix}")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._http:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        request = self._http.build_request(method, url, params=params, json=json)
        await self._auth.apply(request)
        response = await self._http.send(request)

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
        """GET a single entry by ID."""
        return await self._request("GET", self._repo_path(f"Entries/{entry_id}"))

    async def get_entry_by_path(self, full_path: str) -> dict[str, Any]:
        """Resolve a full path like /Imports/2024/Onboarding to an entry."""
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
        """List children of a folder."""
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
        """Run a Laserfiche search query.

        Query syntax follows Laserfiche search syntax, e.g.:
            {LF:Name="Onboarding*"}
            {[Missionary Application]:[Last Name]="Smith"}
        """
        return await self._request(
            "GET",
            self._repo_path("Entries/SearchEntries"),
            params={"searchCommand": query, "$top": max_results},
        )

    async def get_field_values(self, entry_id: int) -> dict[str, Any]:
        """Read template fields assigned to an entry."""
        return await self._request(
            "GET",
            self._repo_path(f"Entries/{entry_id}/Fields"),
        )

    async def get_entry_content(self, entry_id: int) -> bytes:
        """Download an electronic document's raw bytes."""
        if not self._http:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")
        url = self._repo_path(f"Entries/{entry_id}/Edoc")
        request = self._http.build_request("GET", url)
        await self._auth.apply(request)
        response = await self._http.send(request)
        if response.status_code >= 400:
            raise LaserficheError(
                f"Failed to download entry {entry_id}: HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return response.content
