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


# --- content search (async /Searches flow) -----------------------------------


class ContextHit(BaseModel):
    """One matched passage inside a document's indexed (often OCR'd) text.

    Only the asynchronous ``/Searches`` flow produces these — ``SimpleSearches``
    tells you an entry matched, not where.
    """

    page: int | None = Field(
        default=None,
        description="1-based page the match sits on. Null for non-paginated hits.",
    )
    text: str = Field(description="Text surrounding the match, as indexed.")
    match: str | None = Field(
        default=None,
        description="The exact substring that matched, sliced out of `text`.",
    )
    hit_type: str | None = Field(
        default=None,
        description="Where the match came from: PageContent, Field, Annotation, ...",
    )
    field_name: str | None = Field(
        default=None,
        description="Template field the match came from, when hit_type is a field hit.",
    )

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, context_chars: int) -> ContextHit:
        """Build a hit from a ``SearchContextHit`` payload.

        The server locates the match within ``context`` via
        ``highlight1Offset`` / ``highlight1Length``. We slice that span out
        into ``match`` rather than wrapping it in markers, so the surrounding
        text stays byte-identical to what is stored in the index.

        ``context`` is then trimmed to ``context_chars`` *centered on the
        match*, so a server that returns a whole page still costs a
        predictable number of tokens without cutting the match itself off.
        """
        context = str(_pick(raw, "context", "Context", default="") or "")
        offset = _pick(raw, "highlight1Offset", "Highlight1Offset", default=0) or 0
        length = _pick(raw, "highlight1Length", "Highlight1Length", default=0) or 0

        match: str | None = None
        if isinstance(offset, int) and isinstance(length, int) and length > 0:
            candidate = context[offset : offset + length]
            match = candidate or None

        page_raw = _pick(raw, "pageNumber", "PageNumber")
        # Field and annotation hits report page 0 — that's "not on a page",
        # not "page zero", so don't surface it as a page number.
        page = page_raw if isinstance(page_raw, int) and page_raw > 0 else None

        field_name = _pick(raw, "fieldName", "FieldName") or None

        return cls(
            page=page,
            text=_center_trim(context, offset, length, context_chars),
            match=match,
            hit_type=_pick(raw, "hitType", "HitType"),
            field_name=field_name,
        )


def _center_trim(text: str, offset: int, length: int, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars, keeping the highlighted span inside.

    Ellipses mark where text was removed so the model can tell a trimmed
    excerpt from a complete one. If the span itself is longer than ``limit``,
    the span wins — truncating the match would defeat the point.
    """
    if len(text) <= limit:
        return text

    if not isinstance(offset, int) or not isinstance(length, int) or length <= 0:
        return text[:limit].rstrip() + "…"

    # Center the window on the match, then clamp it into range.
    slack = max(0, limit - length)
    start = max(0, offset - slack // 2)
    end = min(len(text), start + limit)
    start = max(0, end - limit)

    excerpt = text[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{excerpt}{suffix}"


class ContentSearchResult(BaseModel):
    """One matching entry plus the passages that matched inside it."""

    entry_id: int
    name: str
    entry_type: EntryType
    full_path: str | None = None
    row_number: int | None = Field(
        default=None,
        description="1-based position in the full result set; the key context hits are fetched by.",
    )
    hits: list[ContextHit] = Field(default_factory=list)
    hit_count: int | None = Field(
        default=None,
        description="Total passages that matched in this entry, before `hits` was truncated.",
    )
    hits_truncated: bool = False
    hits_error: str | None = Field(
        default=None,
        description="Set when this row's context hits could not be fetched; "
        "the entry still matched.",
    )

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ContentSearchResult:
        summary = EntrySummary.from_api(raw)
        row = _pick(raw, "rowNumber", "RowNumber")
        return cls(
            entry_id=summary.id,
            name=summary.name,
            entry_type=summary.entry_type,
            full_path=summary.full_path,
            row_number=row if isinstance(row, int) else None,
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
