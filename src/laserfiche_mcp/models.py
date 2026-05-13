"""Pydantic models representing Laserfiche entities.

Models intentionally surface a subset of the full Repository API response —
only fields likely to be useful to the LLM. This keeps token usage low and
tool responses scannable.

Each model exposes a ``from_api`` classmethod that translates a raw API
payload into the trimmed shape, tolerating both camelCase and PascalCase
keys (the Repository API has been inconsistent across versions).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def _pick(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first key in ``keys`` that's present in ``raw``.

    Unlike ``raw.get(k1) or raw.get(k2)``, this distinguishes "missing" from
    "explicitly falsy" — important when the API legitimately returns ``0``,
    ``""``, ``False``, or empty list/dict for a field.
    """
    for key in keys:
        if key in raw:
            return raw[key]
    return default


class EntryType(str, Enum):
    FOLDER = "Folder"
    DOCUMENT = "Document"
    SHORTCUT = "Shortcut"
    RECORD_SERIES = "RecordSeries"
    UNKNOWN = "Unknown"

    @classmethod
    def coerce(cls, raw: str | None) -> EntryType:
        if not raw:
            return cls.UNKNOWN
        try:
            return cls(raw)
        except ValueError:
            return cls.UNKNOWN


class EntrySummary(BaseModel):
    """Lightweight entry representation for list/search results."""

    id: int = Field(description="Laserfiche entry ID.")
    name: str
    entry_type: EntryType
    parent_id: int | None = None
    full_path: str | None = None
    creation_time: datetime | None = None
    last_modified_time: datetime | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> EntrySummary:
        return cls(
            id=_pick(raw, "id", "Id", default=0),
            name=_pick(raw, "name", "Name", default=""),
            entry_type=EntryType.coerce(_pick(raw, "entryType", "EntryType")),
            parent_id=_pick(raw, "parentId", "ParentId"),
            full_path=_pick(raw, "fullPath", "FullPath"),
            creation_time=_pick(raw, "creationTime", "CreationTime"),
            last_modified_time=_pick(raw, "lastModifiedTime", "LastModifiedTime"),
        )


class FieldValue(BaseModel):
    """A template field assigned to an entry."""

    field_name: str
    field_type: str | None = None
    values: list[Any] = Field(default_factory=list)
    is_multi_value: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> FieldValue:
        return cls(
            field_name=_pick(raw, "fieldName", "FieldName", default=""),
            field_type=_pick(raw, "fieldType", "FieldType"),
            values=_pick(raw, "values", "Values", default=[]),
            is_multi_value=bool(_pick(raw, "isMultiValue", "IsMultiValue", default=False)),
        )

    @classmethod
    def list_from_api(cls, raw: dict[str, Any]) -> list[FieldValue]:
        items = _pick(raw, "value", "Value", default=[])
        return [cls.from_api(item) for item in items]


class EntryDetail(EntrySummary):
    """Full entry detail including template and fields."""

    template_name: str | None = None
    fields: list[FieldValue] = Field(default_factory=list)
    page_count: int | None = None
    is_electronic_document: bool | None = None
    extension: str | None = None

    @classmethod
    def from_api(
        cls,
        raw: dict[str, Any],
        fields: list[FieldValue] | None = None,
    ) -> EntryDetail:
        summary = EntrySummary.from_api(raw)
        return cls(
            **summary.model_dump(),
            template_name=_pick(raw, "templateName", "TemplateName"),
            fields=fields or [],
            page_count=_pick(raw, "pageCount", "PageCount"),
            is_electronic_document=_pick(raw, "isElectronicDocument", "IsElectronicDocument"),
            extension=_pick(raw, "extension", "Extension"),
        )


class SearchResults(BaseModel):
    """Container for search-style responses with paging hints."""

    entries: list[EntrySummary]
    total_count: int | None = None
    next_link: str | None = Field(
        default=None,
        description="Opaque continuation token; pass to next call to get more results.",
    )

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SearchResults:
        items = _pick(raw, "value", "Value", default=[])
        return cls(
            entries=[EntrySummary.from_api(item) for item in items],
            total_count=raw.get("@odata.count"),
            next_link=raw.get("@odata.nextLink"),
        )


# --- search_natural ----------------------------------------------------------


class CandidateQuery(BaseModel):
    """A Laserfiche query string suggested by Mode A guidance."""

    query: str
    rationale: str = Field(
        description="Plain-English explanation of why this query was suggested.",
    )


class TemplateHint(BaseModel):
    """A template observed in the sampled folder plus its field names."""

    template_name: str
    field_names: list[str] = Field(default_factory=list)


class SearchAttempt(BaseModel):
    """One round of a Mode B query attempt — what was sent, what came back."""

    query: str
    repair: str | None = Field(
        default=None,
        description="If this attempt was an automatic repair, what kind "
        "(e.g. 'escape_quotes', 'wildcard_wrap'). null for the first attempt.",
    )
    status_code: int | None = None
    error_body: Any = None


class SearchNaturalResponse(BaseModel):
    """Discriminated response from search_natural.

    ``mode`` says which shape to read:

    * ``"guidance"`` — Mode A. The LLM did not provide ``lf_query`` yet. Read
      ``grammar``, ``discovered_templates``, ``candidate_queries`` and the
      ``follow_up`` hint, then call search_natural again with one of the
      candidates (or refine it) as ``lf_query``.
    * ``"results"`` — Mode B succeeded. Read ``entries``. ``repairs_applied``
      lists any automatic repairs taken. ``pagination_unknown=true`` means
      ``next_link`` was null but the result count hit the cap — there may be
      more, the server just didn't say.
    * ``"error"`` — Mode B failed. Read ``attempts`` (one per repair tried)
      and ``next_action`` for guidance on how to fix the query.
    """

    mode: Literal["guidance", "results", "error"]
    question: str

    # --- guidance fields -----------------------------------------------------
    folder_path: str | None = None
    grammar: str | None = None
    discovered_templates: list[TemplateHint] = Field(default_factory=list)
    candidate_queries: list[CandidateQuery] = Field(default_factory=list)
    follow_up: str | None = None
    notes: list[str] = Field(default_factory=list)

    # --- results fields ------------------------------------------------------
    lf_query: str | None = None
    repairs_applied: list[str] = Field(default_factory=list)
    entries: list[EntrySummary] = Field(default_factory=list)
    total_count: int | None = None
    next_link: str | None = None
    pagination_unknown: bool = False
    effective_max_results: int | None = None

    # --- error fields --------------------------------------------------------
    attempts: list[SearchAttempt] = Field(default_factory=list)
    final_error: str | None = None
    next_action: str | None = None
