"""Thin async client for the Laserfiche Repository API (v1 and v2).

Endpoint paths follow the official self-hosted Repository API conventions
as documented at developer.laserfiche.com and confirmed against the on-server
OpenAPI spec (``/swagger/v1/swagger.json``) plus the official
``Laserfiche/lf-repository-api-client-java`` reference client.

Path summary, relative to ``/{api_version}/Repositories/{repositoryId}/``.

v1 (older self-hosted builds — current default):
  GET  Entries/{id}                                          — get entry
  GET  Entries/ByPath?fullPath=...                           — resolve path
  GET  Entries/{id}/Laserfiche.Repository.Folder/children    — list folder
  GET  Entries/{id}/fields                                   — field values
  POST SimpleSearches                                        — simple search
  GET  Entries/{id}/Laserfiche.Repository.Document/edoc      — raw edoc bytes
  (no endpoint)                                              — extracted text

v2 (newer self-hosted builds):
  GET  Entries/{id}                                          — get entry
  GET  Entries/ByPath?fullPath=...                           — resolve path
  GET  Entries/{id}/Folder/Children                          — list folder
  GET  Entries/{id}/Fields                                   — field values
  POST SimpleSearches                                        — simple search
  POST Entries/{id}/Export {"part": "Edoc"}                  — raw edoc bytes
  POST Entries/{id}/Export {"part": "Text"}                  — extracted text

Search on both versions uses POST with a JSON body ``{"searchCommand": "<q>"}``,
NOT a GET with a query string. Version is selected by ``Settings.api_version``
(``LF_API_VERSION``), default ``v1``.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from typing import Any
from urllib.parse import urljoin

import httpx

from .auth import AuthStrategy
from .config import ApiVersion, Settings

logger = logging.getLogger("laserfiche_mcp.client")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LaserficheError(Exception):
    """Raised when the Repository API returns an error or unexpected response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def build_repo_path(
    base_url: str,
    repository_id: str,
    suffix: str,
    api_version: ApiVersion = ApiVersion.V1,
) -> str:
    """Construct a /{api_version}/Repositories/{repo}/{suffix} URL.

    Pulled out of ``LaserficheClient`` so it's directly unit-testable.
    """
    if not base_url.endswith("/"):
        base_url += "/"
    suffix = suffix.lstrip("/")
    return urljoin(
        base_url, f"{api_version.value}/Repositories/{repository_id}/{suffix}"
    )


class LaserficheClient:
    """Async client for the self-hosted Repository API (v1 or v2)."""

    def __init__(self, settings: Settings, auth: AuthStrategy) -> None:
        self._settings = settings
        self._auth = auth
        self._base_url = str(settings.repo_api_url) if settings.repo_api_url else ""
        self._repository_id = settings.repository_id or ""
        self._api_version = settings.api_version
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
        return build_repo_path(
            self._base_url, self._repository_id, suffix, self._api_version
        )

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

    async def _request_json(
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

    async def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> bytes:
        content, _ = await self._request_bytes_with_meta(method, url, json=json)
        return content

    async def _request_bytes_with_meta(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> tuple[bytes, str | None]:
        """Like ``_request_bytes`` but also surfaces the response Content-Type.

        Needed by edoc modes that branch on document type (PDF vs text vs
        binary) instead of trusting the file extension on the entry.
        """
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        request = self._http.build_request(method, url, json=json)
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
        return response.content, response.headers.get("content-type")

    # --- Read operations (v1) ---------------------------------------------

    async def get_entry(self, entry_id: int) -> dict[str, Any]:
        """GET /Entries/{entryId}"""
        return await self._request_json("GET", self._repo_path(f"Entries/{entry_id}"))

    async def get_entry_by_path(self, full_path: str) -> dict[str, Any]:
        """GET /Entries/ByPath?fullPath={path}"""
        return await self._request_json(
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
        """List immediate children of a folder.

        v1: GET /Entries/{id}/Laserfiche.Repository.Folder/children
        v2: GET /Entries/{id}/Folder/Children
        """
        if self._api_version is ApiVersion.V1:
            suffix = f"Entries/{folder_id}/Laserfiche.Repository.Folder/children"
        else:
            suffix = f"Entries/{folder_id}/Folder/Children"
        return await self._request_json(
            "GET",
            self._repo_path(suffix),
            params={"$top": max_results, "$skip": skip},
        )

    async def search_entries(
        self,
        query: str,
        *,
        max_results: int = 25,
    ) -> dict[str, Any]:
        """POST /SimpleSearches with body {"searchCommand": "<query>"}.

        Query syntax follows Laserfiche search syntax, e.g.:
            {LF:Name="Onboarding*"}
            {[Missionary Application]:[Last Name]="Smith"}
        """
        return await self._request_json(
            "POST",
            self._repo_path("SimpleSearches"),
            params={"$top": max_results},
            json={"searchCommand": query},
        )

    async def get_field_values(self, entry_id: int) -> dict[str, Any]:
        """Read template field values on an entry.

        v1: GET /Entries/{id}/fields   (lowercase)
        v2: GET /Entries/{id}/Fields   (PascalCase)
        """
        segment = "fields" if self._api_version is ApiVersion.V1 else "Fields"
        return await self._request_json(
            "GET",
            self._repo_path(f"Entries/{entry_id}/{segment}"),
        )

    async def export_entry(
        self,
        entry_id: int,
        *,
        part: str = "Edoc",
    ) -> bytes:
        """Download document content.

        v2 uses a unified POST /Entries/{id}/Export with body
        ``{"part": "Edoc"|"Text"|"Image"}``.

        v1 has no unified Export endpoint. Only Edoc is supported, via
        GET /Entries/{id}/Laserfiche.Repository.Document/edoc. Text and
        Image parts have no v1 equivalent and raise ``LaserficheError``.
        """
        content, _ = await self.export_entry_with_meta(entry_id, part=part)
        return content

    async def export_entry_with_meta(
        self,
        entry_id: int,
        *,
        part: str = "Edoc",
    ) -> tuple[bytes, str | None]:
        """Like :meth:`export_entry` but also returns the response Content-Type.

        Same v1/v2 routing rules apply. v1 only supports ``part='Edoc'``.
        """
        if self._api_version is ApiVersion.V1:
            if part != "Edoc":
                raise LaserficheError(
                    f"Laserfiche API v1 has no endpoint for downloading "
                    f"part={part!r}. Only 'Edoc' (raw electronic document "
                    f"bytes) is supported on v1; set LF_API_VERSION=v2 if "
                    f"your server supports it."
                )
            return await self._request_bytes_with_meta(
                "GET",
                self._repo_path(
                    f"Entries/{entry_id}/Laserfiche.Repository.Document/edoc"
                ),
            )

        return await self._request_bytes_with_meta(
            "POST",
            self._repo_path(f"Entries/{entry_id}/Export"),
            json={"part": part},
        )
