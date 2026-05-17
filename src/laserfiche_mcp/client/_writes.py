"""All state-mutating endpoints: patch, delete, create, copy, import, put, template."""

from __future__ import annotations

import json as _json
from typing import Any, cast
from urllib.parse import quote as _url_quote

from ..config import ApiVersion
from ..errors import LaserficheError
from ._core import _CoreClient


class _WritesMixin(_CoreClient):
    """Endpoints that mutate state — gated behind ``LF_READ_ONLY`` at the tool layer."""

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
            self._repo_path(f"Entries/{parent_id}/Laserfiche.Repository.Folder/CopyAsync"),
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

        url = self._repo_path(f"Entries/{parent_id}/{_url_quote(new_entry_name, safe='')}")
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
        return cast(dict[str, Any], response.json())

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
            # _request_json's `json` param is typed as dict | None, but this
            # endpoint specifically expects a bare list. Bypass the type check
            # rather than widen the helper signature for one caller.
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
