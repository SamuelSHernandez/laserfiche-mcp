"""Thin async client for the Laserfiche Repository API (v1 and v2).

Endpoint paths follow the official self-hosted Repository API conventions
as documented at developer.laserfiche.com and confirmed against the on-server
OpenAPI spec (``/swagger/v1/swagger.json``) plus the official
``Laserfiche/lf-repository-api-client-java`` reference client.

Path summary, relative to ``/{api_version}/Repositories/{repositoryId}/``.

v1 (older self-hosted builds — current default):
  GET    Entries/{id}                                          — get entry
  GET    Entries/ByPath?fullPath=...                           — resolve path
  PATCH  Entries/{id}                                          — move/rename/retemplate
  DELETE Entries/{id}                                          — delete (async)
  POST   Entries/{id}/Folder                                   — create child folder / copy
  POST   Entries/{id}/{newName}                                — import document (multipart)
  GET    Entries/{id}/Laserfiche.Repository.Folder/children    — list folder
  GET    Entries/{id}/fields                                   — field values
  PUT    Entries/{id}/fields                                   — overwrite fields
  GET    Entries/{id}/tags                                     — tags
  PUT    Entries/{id}/tags                                     — overwrite tags
  GET    Entries/{id}/links                                    — links
  PUT    Entries/{id}/links                                    — overwrite links
  PUT    Entries/{id}/template                                 — assign template
  DELETE Entries/{id}/template                                 — clear template
  GET    Entries/{id}/Laserfiche.Repository.Document/edoc      — raw edoc bytes
  DELETE Entries/{id}/edoc                                     — delete edoc
  DELETE Entries/{id}/pages?pageRange=...                      — delete pages
  POST   SimpleSearches                                        — simple search
  GET    FieldDefinitions, TagDefinitions, TemplateDefinitions, LinkDefinitions, AuditReasons
  GET    Tasks/{operationToken}                                — async op status
  (no endpoint)                                                — extracted text

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
import json as _json
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
    """Raised when the Repository API returns an error or unexpected response.

    ``detail`` carries the parsed response body (dict if JSON, str if
    plaintext) so callers can extract ``errorCode``, ``title``, etc.
    without re-parsing the message string.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        detail: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


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
                detail=detail,
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
                detail=detail,
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
        include_count: bool = False,
    ) -> dict[str, Any]:
        """List immediate children of a folder.

        v1: GET /Entries/{id}/Laserfiche.Repository.Folder/children
        v2: GET /Entries/{id}/Folder/Children

        Set ``include_count=True`` to add ``$count=true`` so the response
        includes ``@odata.count`` (the total number of children, not just
        the page size). Off by default since most callers don't need it
        and the count is computed server-side.
        """
        if self._api_version is ApiVersion.V1:
            suffix = f"Entries/{folder_id}/Laserfiche.Repository.Folder/children"
        else:
            suffix = f"Entries/{folder_id}/Folder/Children"
        params: dict[str, Any] = {"$top": max_results, "$skip": skip}
        if include_count:
            params["$count"] = "true"
        return await self._request_json(
            "GET",
            self._repo_path(suffix),
            params=params,
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

    # --- Definitions and audit reads --------------------------------------

    async def list_repositories(self) -> dict[str, Any]:
        """GET /{version}/Repositories.

        Unlike other routes this has no per-repository prefix — it lists
        every repository the authenticated user can see.
        """
        base = self._base_url if self._base_url.endswith("/") else self._base_url + "/"
        url = urljoin(base, f"{self._api_version.value}/Repositories")
        return await self._request_json("GET", url)

    async def list_field_definitions(
        self, *, max_results: int = 100, skip: int = 0,
    ) -> dict[str, Any]:
        """GET /FieldDefinitions — every field definition in the repo."""
        return await self._request_json(
            "GET",
            self._repo_path("FieldDefinitions"),
            params={"$top": max_results, "$skip": skip},
        )

    async def list_tag_definitions(
        self, *, max_results: int = 100, skip: int = 0,
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
            "GET", self._repo_path("TemplateDefinitions"), params=params,
        )

    async def list_link_definitions(
        self, *, max_results: int = 100, skip: int = 0,
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
            "GET", self._repo_path(f"Tasks/{operation_token}"),
        )

    # --- Tags and links reads (needed by merge helpers) -------------------

    async def get_tags(self, entry_id: int) -> dict[str, Any]:
        """GET /Entries/{id}/tags — current tag assignments."""
        segment = "tags" if self._api_version is ApiVersion.V1 else "Tags"
        return await self._request_json(
            "GET", self._repo_path(f"Entries/{entry_id}/{segment}"),
        )

    async def get_links(self, entry_id: int) -> dict[str, Any]:
        """GET /Entries/{id}/links — current link assignments."""
        segment = "links" if self._api_version is ApiVersion.V1 else "Links"
        return await self._request_json(
            "GET", self._repo_path(f"Entries/{entry_id}/{segment}"),
        )

    # --- Write operations -------------------------------------------------

    async def patch_entry(
        self,
        entry_id: int,
        *,
        parent_id: int | None = None,
        name: str | None = None,
        template_name: str | None = None,
        template_id: int | None = None,
        fields: dict[str, Any] | None = None,
        auto_rename: bool = False,
    ) -> dict[str, Any]:
        """PATCH /Entries/{id} — move and/or rename and/or retemplate.

        Pass only the parts you want to change. Returns the updated Entry.
        """
        body: dict[str, Any] = {}
        if parent_id is not None:
            body["parentId"] = parent_id
        if name is not None:
            body["name"] = name
        if template_name is not None:
            body["templateName"] = template_name
        if template_id is not None:
            body["templateId"] = template_id
        if fields is not None:
            body["fields"] = fields
        return await self._request_json(
            "PATCH",
            self._repo_path(f"Entries/{entry_id}"),
            params={"autoRename": str(auto_rename).lower()},
            json=body,
        )

    async def delete_entry(
        self,
        entry_id: int,
        *,
        audit_reason_id: int | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """DELETE /Entries/{id} — queue an async delete.

        Returns ``AcceptedOperation`` (``{ token, taskId }``). Poll
        :meth:`get_task_status` to observe completion.
        """
        body: dict[str, Any] = {}
        if audit_reason_id is not None:
            body["auditReasonId"] = audit_reason_id
        if comment is not None:
            body["comment"] = comment
        # Always send a JSON body (even if empty) so httpx attaches the
        # Content-Type: application/json header. LFRepositoryAPI v1 returns
        # HTTP 415 on DELETE when Content-Type is missing.
        return await self._request_json(
            "DELETE",
            self._repo_path(f"Entries/{entry_id}"),
            json=body,
        )

    async def create_child_entry(
        self,
        parent_id: int,
        *,
        entry_type: str,
        name: str,
        template_name: str | None = None,
        fields: dict[str, Any] | None = None,
        source_id: int | None = None,
        auto_rename: bool = False,
    ) -> dict[str, Any]:
        """POST a child entry create/copy/shortcut to the parent.

        v1: POST /Entries/{parentId}/Laserfiche.Repository.Folder/children
        v2: POST /Entries/{parentId}/Folder

        ``entry_type`` controls intent:
          * ``"Folder"`` — create a new folder
          * ``"Shortcut"`` — create a shortcut (pass ``source_id``)
          * ``"Document"`` and a ``source_id`` — copy an existing entry
        """
        body: dict[str, Any] = {"entryType": entry_type, "name": name}
        if template_name is not None:
            body["templateName"] = template_name
        if fields is not None:
            body["fields"] = fields
        if source_id is not None:
            body["sourceId"] = source_id
        if self._api_version is ApiVersion.V1:
            suffix = f"Entries/{parent_id}/Laserfiche.Repository.Folder/children"
        else:
            suffix = f"Entries/{parent_id}/Folder"
        return await self._request_json(
            "POST",
            self._repo_path(suffix),
            params={"autoRename": str(auto_rename).lower()},
            json=body,
        )

    async def copy_entry_async(
        self,
        parent_id: int,
        *,
        source_id: int,
        name: str,
        volume_name: str | None = None,
        auto_rename: bool = False,
    ) -> dict[str, Any]:
        """POST /Entries/{parentId}/Laserfiche.Repository.Folder/CopyAsync.

        Async-only on v1. The 201 response is ``AcceptedOperation``
        (``{token: "..."}``); poll :meth:`get_task_status` to observe
        completion. Distinct from the synchronous create/shortcut route,
        whose ``PostEntryChildrenEntryType`` enum doesn't accept
        ``Document``.
        """
        body: dict[str, Any] = {"name": name, "sourceId": source_id}
        if volume_name is not None:
            body["volumeName"] = volume_name
        return await self._request_json(
            "POST",
            self._repo_path(
                f"Entries/{parent_id}/Laserfiche.Repository.Folder/CopyAsync"
            ),
            params={"autoRename": str(auto_rename).lower()},
            json=body,
        )

    async def import_document(
        self,
        parent_id: int,
        new_entry_name: str,
        file_bytes: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
        auto_rename: bool = False,
    ) -> dict[str, Any]:
        """POST /Entries/{parentId}/{newEntryName} — multipart upload.

        Body is multipart with ``electronicDocument`` (the file) and
        optional ``request`` (JSON metadata). The server may return a
        ``CreateEntryResult`` with per-operation statuses — partial
        success is possible (entry created even if e.g. setLinks failed).
        """
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        from urllib.parse import quote as _quote
        url = self._repo_path(f"Entries/{parent_id}/{_quote(new_entry_name, safe='')}")
        files: dict[str, Any] = {
            "electronicDocument": (new_entry_name, file_bytes, content_type),
        }
        if metadata:
            # The metadata JSON travels as a separate form part named "request".
            files["request"] = (None, _json.dumps(metadata), "application/json")

        request = self._http.build_request(
            "POST",
            url,
            params={"autoRename": str(auto_rename).lower()},
            files=files,
        )
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
        return response.json()

    async def put_fields(
        self,
        entry_id: int,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """PUT /Entries/{id}/fields — OVERWRITE field values.

        Excluded fields are deleted from the entry; templated fields not in
        the body are reset to empty (template requirement keeps them
        assigned). See ``server.merge_fields`` for a safer GET+merge wrapper.

        v1 body shape per the on-server swagger: a flat
        ``{FieldName: FieldToUpdate}`` dict, NOT wrapped in a ``fields`` key.
        """
        segment = "fields" if self._api_version is ApiVersion.V1 else "Fields"
        if self._api_version is ApiVersion.V1:
            body: dict[str, Any] = fields
        else:
            body = {"fields": fields}
        return await self._request_json(
            "PUT",
            self._repo_path(f"Entries/{entry_id}/{segment}"),
            json=body,
        )

    async def put_tags(
        self,
        entry_id: int,
        tags: list[str],
    ) -> dict[str, Any]:
        """PUT /Entries/{id}/tags — OVERWRITE tags."""
        segment = "tags" if self._api_version is ApiVersion.V1 else "Tags"
        return await self._request_json(
            "PUT",
            self._repo_path(f"Entries/{entry_id}/{segment}"),
            json={"tags": tags},
        )

    async def put_links(
        self,
        entry_id: int,
        links: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """PUT /Entries/{id}/links — OVERWRITE links.

        The v1 docs describe ``PutLinksRequest`` as a bare array of
        ``{ targetId, linkTypeId }`` objects (unlike PutFields/PutTags
        which wrap their array in an object). We pass it as such.
        """
        segment = "links" if self._api_version is ApiVersion.V1 else "Links"
        return await self._request_json(
            "PUT",
            self._repo_path(f"Entries/{entry_id}/{segment}"),
            json=links,  # type: ignore[arg-type]
        )

    async def assign_template(
        self,
        entry_id: int,
        template_name: str,
        *,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """PUT /Entries/{id}/template — assign or change template.

        Per the API docs: only template-scoped values are modified; existing
        independent fields stay. Fields common to the previously and newly
        assigned templates retain their values.
        """
        body: dict[str, Any] = {"templateName": template_name}
        if fields is not None:
            body["fields"] = fields
        return await self._request_json(
            "PUT",
            self._repo_path(f"Entries/{entry_id}/template"),
            json=body,
        )

    async def remove_template(self, entry_id: int) -> dict[str, Any]:
        """DELETE /Entries/{id}/template — clear template assignment."""
        return await self._request_json(
            "DELETE",
            self._repo_path(f"Entries/{entry_id}/template"),
        )

    async def delete_edoc(self, entry_id: int) -> dict[str, Any]:
        """DELETE the electronic document content. Entry itself remains.

        v1: DELETE /Entries/{id}/Laserfiche.Repository.Document/edoc
        v2: DELETE /Entries/{id}/edoc
        """
        if self._api_version is ApiVersion.V1:
            suffix = f"Entries/{entry_id}/Laserfiche.Repository.Document/edoc"
        else:
            suffix = f"Entries/{entry_id}/edoc"
        # Send an empty JSON body so httpx attaches Content-Type — v1
        # otherwise returns 415 (same cause as delete_entry).
        return await self._request_json(
            "DELETE",
            self._repo_path(suffix),
            json={},
        )

    async def delete_pages(
        self,
        entry_id: int,
        page_range: str,
    ) -> dict[str, Any]:
        """DELETE /Entries/{id}/pages?pageRange=... — remove specific pages.

        ``page_range`` examples: ``"1,2,3"``, ``"1-3,5"``, ``"2-7,10-12"``.

        The API treats an empty ``pageRange`` as "delete every page".
        This client refuses empty values to remove that footgun — callers
        that really want to wipe all pages can pass ``"1-9999"`` (or
        whatever exceeds the page count).
        """
        if not page_range or not page_range.strip():
            raise LaserficheError(
                "delete_pages requires a non-empty page_range. The API treats "
                "an empty range as 'delete all pages', which is too easy to "
                "fat-finger; pass an explicit range like '1-9999' if you "
                "really intended to delete every page."
            )
        if self._api_version is ApiVersion.V1:
            suffix = f"Entries/{entry_id}/Laserfiche.Repository.Document/pages"
        else:
            suffix = f"Entries/{entry_id}/pages"
        return await self._request_json(
            "DELETE",
            self._repo_path(suffix),
            params={"pageRange": page_range},
            json={},
        )
