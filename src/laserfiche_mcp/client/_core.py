"""Core transport for :class:`LaserficheClient` — auth, retry, request helpers.

Holds the per-instance state (``_settings``, ``_auth``, ``_http``, schema
caches) and the request primitives (``_send``, ``_request_json``,
``_request_bytes``, ``_request_bytes_with_meta``, ``_repo_path``). The
resource mixins in this package extend ``_CoreClient`` so their methods
can call those primitives directly.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from typing import Any, TypeVar, cast
from urllib.parse import urljoin

import httpx

from ..auth import AuthStrategy
from ..config import ApiVersion, Settings
from ..errors import LaserficheError

logger = logging.getLogger("laserfiche_mcp.client")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_CacheValueT = TypeVar("_CacheValueT")
# Bound to ``_CoreClient`` so subclasses' ``__aenter__`` reports the concrete
# subclass type (e.g. ``LaserficheClient``) instead of ``_CoreClient``.
_SelfCore = TypeVar("_SelfCore", bound="_CoreClient")


def build_repo_path(
    base_url: str,
    repository_id: str,
    suffix: str,
    api_version: ApiVersion = ApiVersion.V1,
) -> str:
    """Construct a /{api_version}/Repositories/{repo}/{suffix} URL.

    Pulled out of ``_CoreClient`` so it's directly unit-testable.
    """
    if not base_url.endswith("/"):
        base_url += "/"
    suffix = suffix.lstrip("/")
    return urljoin(base_url, f"{api_version.value}/Repositories/{repository_id}/{suffix}")


class _CoreClient:
    """Transport core. Not used directly — composed into ``LaserficheClient``."""

    def __init__(self, settings: Settings, auth: AuthStrategy) -> None:
        self._settings = settings
        self._auth = auth
        self._base_url = str(settings.repo_api_url) if settings.repo_api_url else ""
        self._repository_id = settings.repository_id or ""
        self._api_version = settings.api_version
        self._http: httpx.AsyncClient | None = None

        # Schema-definition caches for client-side pre-flight validation.
        # Each cache stores (value, expiry_monotonic). TTL is taken from
        # settings.schema_cache_ttl_seconds at lookup time so the env var
        # can be tuned without recreating the client.
        self._field_def_cache: tuple[dict[str, Any], float] | None = None
        self._tag_def_cache: tuple[dict[str, Any], float] | None = None
        self._template_def_cache: tuple[dict[str, Any], float] | None = None
        # Keyed by linkTypeId (int), unlike the other caches which are keyed
        # by name. See ``_DefinitionsMixin.cached_link_definitions``.
        self._link_def_cache: tuple[dict[int, dict[str, Any]], float] | None = None

        if not settings.verify_ssl:
            warnings.warn(
                "TLS certificate verification is DISABLED (LF_VERIFY_SSL=false). "
                "This is insecure outside of trusted internal dev environments.",
                stacklevel=2,
            )

    async def __aenter__(self: _SelfCore) -> _SelfCore:
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

    # --- Path + transport --------------------------------------------------

    def _repo_path(self, suffix: str) -> str:
        return build_repo_path(self._base_url, self._repository_id, suffix, self._api_version)

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
                delay = 2**attempt
                logger.warning(
                    "Network error on %s %s (attempt %d/%d): %s; retrying in %ds",
                    request.method,
                    request.url,
                    attempt + 1,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                delay = 2**attempt
                logger.warning(
                    "Retryable status %d on %s %s (attempt %d/%d); retrying in %ds",
                    response.status_code,
                    request.method,
                    request.url,
                    attempt + 1,
                    attempts,
                    delay,
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
                detail=detail,
            )

        if not response.content:
            return {}
        return cast(dict[str, Any], response.json())

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
                detail=detail,
            )
        return response.content, response.headers.get("content-type")

    # --- Cache helper (used by _DefinitionsMixin) -------------------------

    def _cache_alive(
        self,
        entry: tuple[_CacheValueT, float] | None,
    ) -> _CacheValueT | None:
        """Return the cached value if not expired, else None."""
        import time

        if entry is None:
            return None
        value, expiry = entry
        if time.monotonic() >= expiry:
            return None
        return value
