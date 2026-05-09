"""Pydantic models representing Laserfiche entities.

Models intentionally use a subset of the full Repository API response shape — only
fields likely to be useful to the LLM are surfaced. This keeps token usage low
and tool responses scannable.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EntryType(str, Enum):
    FOLDER = "Folder"
    DOCUMENT = "Document"
    SHORTCUT = "Shortcut"
    RECORD_SERIES = "RecordSeries"
    UNKNOWN = "Unknown"


class EntrySummary(BaseModel):
    """Lightweight entry representation for list/search results."""

    id: int = Field(description="Laserfiche entry ID.")
    name: str
    entry_type: EntryType
    parent_id: int | None = None
    full_path: str | None = None
    creation_time: datetime | None = None
    last_modified_time: datetime | None = None


class FieldValue(BaseModel):
    """A template field assigned to an entry."""

    field_name: str
    field_type: str | None = None
    values: list[Any] = Field(default_factory=list)
    is_multi_value: bool = False


class EntryDetail(EntrySummary):
    """Full entry detail including template and fields."""

    template_name: str | None = None
    fields: list[FieldValue] = Field(default_factory=list)
    page_count: int | None = None
    is_electronic_document: bool | None = None
    extension: str | None = None


class SearchResults(BaseModel):
    """Container for search-style responses with paging hints."""

    entries: list[EntrySummary]
    total_count: int | None = None
    next_link: str | None = Field(
        default=None,
        description="Opaque continuation token; pass to next call to get more results.",
    )
