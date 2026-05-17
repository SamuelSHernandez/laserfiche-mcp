"""Entry-level reads — get / list-folder / search / fields / tags / links / export."""

from __future__ import annotations

from typing import Any

from ..config import ApiVersion
from ..errors import LaserficheError
from ._core import _CoreClient


class _EntriesMixin(_CoreClient):
    """Read-side operations that target a single entry or a folder listing."""

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

    async def get_tags(self, entry_id: int) -> dict[str, Any]:
        """GET /Entries/{id}/tags — current tag assignments."""
        segment = "tags" if self._api_version is ApiVersion.V1 else "Tags"
        return await self._request_json(
            "GET",
            self._repo_path(f"Entries/{entry_id}/{segment}"),
        )

    async def get_links(self, entry_id: int) -> dict[str, Any]:
        """GET /Entries/{id}/links — current link assignments."""
        segment = "links" if self._api_version is ApiVersion.V1 else "Links"
        return await self._request_json(
            "GET",
            self._repo_path(f"Entries/{entry_id}/{segment}"),
        )

    # --- Document export ---------------------------------------------------

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
                self._repo_path(f"Entries/{entry_id}/Laserfiche.Repository.Document/edoc"),
            )

        return await self._request_bytes_with_meta(
            "POST",
            self._repo_path(f"Entries/{entry_id}/Export"),
            json={"part": part},
        )
