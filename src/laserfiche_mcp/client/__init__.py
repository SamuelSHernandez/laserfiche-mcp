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

This module is a package: the 40-method ``LaserficheClient`` class is
composed from a ``_CoreClient`` base (transport, retry, request helpers)
plus three resource mixins (``_EntriesMixin``, ``_DefinitionsMixin``,
``_WritesMixin``). Each lives in its own file so individual concerns
stay under ~250 lines without sacrificing the single-class public API.
"""

from __future__ import annotations

from ..errors import LaserficheError
from ._core import build_repo_path
from ._definitions import _DefinitionsMixin
from ._entries import _EntriesMixin
from ._writes import _WritesMixin


class LaserficheClient(_EntriesMixin, _DefinitionsMixin, _WritesMixin):
    """Async client for the self-hosted Repository API (v1 or v2).

    Use as an async context manager so the underlying ``httpx.AsyncClient``
    is properly opened and closed::

        async with LaserficheClient(settings, auth) as client:
            entry = await client.get_entry(42)

    All public methods come from the resource mixins; this class itself
    only inherits and composes them. See ``_entries.py``, ``_definitions.py``,
    and ``_writes.py`` for the actual method definitions.
    """


__all__ = ["LaserficheClient", "LaserficheError", "build_repo_path"]
