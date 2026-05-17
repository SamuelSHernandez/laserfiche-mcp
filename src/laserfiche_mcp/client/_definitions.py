"""Repository-level definitions, audit reasons, async tasks, plus their caches.

The schema-definition caches live here (rather than on ``_CoreClient``)
because they're tightly coupled to ``list_*_definitions``: each cache
method is a memoized version of one list call. Keeping them together
makes the relationship obvious and avoids cross-mixin call dependencies.
"""

from __future__ import annotations

import time
from typing import Any, cast
from urllib.parse import urljoin

from ..errors import LaserficheError
from ._core import _CoreClient


class _DefinitionsMixin(_CoreClient):
    """Repository-wide reads + their TTL-bounded caches."""

    async def list_repositories(self) -> dict[str, Any]:
        """GET /{version}/Repositories.

        Unlike other routes this has no per-repository prefix — it lists
        every repository the authenticated user can see.

        Response shape varies across LFRepositoryAPI builds:

        - Some builds return an OData envelope: ``{"value": [{...}, ...]}``
        - Others return a bare JSON array: ``[{...}, ...]``

        We normalize to the envelope shape so callers always see
        ``result["value"]`` as the list of repos, regardless of build.
        """
        base = self._base_url if self._base_url.endswith("/") else self._base_url + "/"
        url = urljoin(base, f"{self._api_version.value}/Repositories")
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        request = self._http.build_request("GET", url)
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
            return {"value": []}
        body = response.json()
        if isinstance(body, list):
            return {"value": body}
        return cast(dict[str, Any], body)

    async def list_field_definitions(
        self,
        *,
        max_results: int = 100,
        skip: int = 0,
    ) -> dict[str, Any]:
        """GET /FieldDefinitions — every field definition in the repo."""
        return await self._request_json(
            "GET",
            self._repo_path("FieldDefinitions"),
            params={"$top": max_results, "$skip": skip},
        )

    async def list_tag_definitions(
        self,
        *,
        max_results: int = 100,
        skip: int = 0,
    ) -> dict[str, Any]:
        """GET /TagDefinitions."""
        return await self._request_json(
            "GET",
            self._repo_path("TagDefinitions"),
            params={"$top": max_results, "$skip": skip},
        )

    async def list_template_definitions(
        self,
        *,
        template_name: str | None = None,
        max_results: int = 100,
        skip: int = 0,
    ) -> dict[str, Any]:
        """GET /TemplateDefinitions[?templateName=...]."""
        params: dict[str, Any] = {"$top": max_results, "$skip": skip}
        if template_name:
            params["templateName"] = template_name
        return await self._request_json(
            "GET",
            self._repo_path("TemplateDefinitions"),
            params=params,
        )

    async def list_link_definitions(
        self,
        *,
        max_results: int = 100,
        skip: int = 0,
    ) -> dict[str, Any]:
        """GET /LinkDefinitions."""
        return await self._request_json(
            "GET",
            self._repo_path("LinkDefinitions"),
            params={"$top": max_results, "$skip": skip},
        )

    async def get_audit_reasons(self) -> dict[str, Any]:
        """GET /AuditReasons — audit reasons the authenticated user can supply.

        Returned as ``{ deleteEntry: [...], exportDocument: [...] }``.
        """
        return await self._request_json("GET", self._repo_path("AuditReasons"))

    async def get_task_status(self, operation_token: str) -> dict[str, Any]:
        """GET /Tasks/{operationToken} — status of an async op (delete, copy, ...)."""
        return await self._request_json(
            "GET",
            self._repo_path(f"Tasks/{operation_token}"),
        )

    # --- Cached schema-definition lookups (for client-side pre-flight) ----
    #
    # Field / tag / template / link definitions are looked up frequently
    # during write-tool validation (e.g., "is this field name real? is
    # this tag defined?"). The underlying API endpoints are stable but
    # not free; caching keeps validation cheap.
    #
    # TTL is read from settings.schema_cache_ttl_seconds at every call
    # so operators can tune the cache window without recreating the
    # client.

    async def cached_field_definitions(self) -> dict[str, dict[str, Any]]:
        """Cached map of field-name → field-definition dict.

        Underlying GET /FieldDefinitions is paged at $top=500 to fetch
        every field in one round trip on typical repositories. Cache
        expires after ``Settings.schema_cache_ttl_seconds``.
        """
        cached = self._cache_alive(self._field_def_cache)
        if cached is not None:
            return cached
        raw = await self.list_field_definitions(max_results=500, skip=0)
        result = {(fd.get("name") or ""): fd for fd in (raw.get("value") or []) if fd.get("name")}
        ttl = self._settings.schema_cache_ttl_seconds
        self._field_def_cache = (result, time.monotonic() + ttl)
        return result

    async def cached_tag_definitions(self) -> dict[str, dict[str, Any]]:
        """Cached map of tag-name → tag-definition dict."""
        cached = self._cache_alive(self._tag_def_cache)
        if cached is not None:
            return cached
        raw = await self.list_tag_definitions(max_results=500, skip=0)
        result = {(td.get("name") or ""): td for td in (raw.get("value") or []) if td.get("name")}
        ttl = self._settings.schema_cache_ttl_seconds
        self._tag_def_cache = (result, time.monotonic() + ttl)
        return result

    async def cached_template_definitions(self) -> dict[str, dict[str, Any]]:
        """Cached map of template-name → template-definition dict."""
        cached = self._cache_alive(self._template_def_cache)
        if cached is not None:
            return cached
        raw = await self.list_template_definitions(max_results=500, skip=0)
        result = {(td.get("name") or ""): td for td in (raw.get("value") or []) if td.get("name")}
        ttl = self._settings.schema_cache_ttl_seconds
        self._template_def_cache = (result, time.monotonic() + ttl)
        return result

    async def cached_link_definitions(self) -> dict[int, dict[str, Any]]:
        """Cached map of linkTypeId → link-definition dict."""
        cached = self._cache_alive(self._link_def_cache)
        if cached is not None:
            return cached
        raw = await self.list_link_definitions(max_results=500, skip=0)
        result: dict[int, dict[str, Any]] = {}
        for ld in raw.get("value") or []:
            ltid = ld.get("linkTypeId")
            if isinstance(ltid, int):
                result[ltid] = ld
        ttl = self._settings.schema_cache_ttl_seconds
        self._link_def_cache = (result, time.monotonic() + ttl)
        return result

    def invalidate_schema_caches(self) -> None:
        """Drop all cached schema definitions. Useful from tests and
        for manual cache invalidation when the operator knows the
        schema has changed."""
        self._field_def_cache = None
        self._tag_def_cache = None
        self._template_def_cache = None
        self._link_def_cache = None
