"""Walk a folder tree once and write the result somewhere useful.

The pattern this replaces: an agent listing a folder, then listing each
subfolder, then fetching each entry — hundreds of round-trips whose results
all land in the context window, most of them never read.

Walking once and writing CSV or JSONL to disk turns that into a file the
user can open in Excel, pipe to ``jq``, or grep. The caller gets a summary
small enough to read aloud.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..models import EntrySummary

# Field order for CSV output. Explicit so the column layout is stable across
# releases — people build spreadsheets on top of this.
CSV_COLUMNS = (
    "entry_id",
    "name",
    "entry_type",
    "parent_id",
    "depth",
    "full_path",
    "extension",
    "template_name",
    "creation_time",
    "last_modified_time",
)


class _FolderLister(Protocol):
    """The single client method this module needs. Keeps the walk testable."""

    async def list_folder(
        self, folder_id: int, *, max_results: int, skip: int, include_count: bool = False
    ) -> dict[str, Any]: ...


@dataclass
class ManifestRow:
    """One entry in the walk, flattened for tabular output."""

    entry_id: int
    name: str
    entry_type: str
    parent_id: int | None
    depth: int
    full_path: str | None = None
    extension: str | None = None
    template_name: str | None = None
    creation_time: str | None = None
    last_modified_time: str | None = None


@dataclass
class ManifestSummary:
    """What the caller reads instead of the rows themselves."""

    total: int = 0
    folders: int = 0
    documents: int = 0
    max_depth: int = 0
    by_extension: dict[str, int] = field(default_factory=dict)
    by_template: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    folders_unreadable: list[int] = field(default_factory=list)
    """Folders whose listing failed (usually permissions). Recorded, not raised —
    an audit that silently skipped a subtree is worse than one that says so."""


def _row_from_api(raw: dict[str, Any], depth: int) -> ManifestRow:
    summary = EntrySummary.from_api(raw)
    return ManifestRow(
        entry_id=summary.id,
        name=summary.name,
        entry_type=summary.entry_type.value
        if hasattr(summary.entry_type, "value")
        else str(summary.entry_type),
        parent_id=summary.parent_id,
        depth=depth,
        full_path=summary.full_path,
        extension=raw.get("extension") or raw.get("Extension"),
        template_name=raw.get("templateName") or raw.get("TemplateName"),
        creation_time=str(summary.creation_time) if summary.creation_time else None,
        last_modified_time=str(summary.last_modified_time) if summary.last_modified_time else None,
    )


async def _list_all_children(
    client: _FolderLister,
    folder_id: int,
    *,
    page_size: int,
) -> list[dict[str, Any]]:
    """Page through a folder's children until the server stops returning any."""
    out: list[dict[str, Any]] = []
    skip = 0
    while True:
        raw = await client.list_folder(folder_id, max_results=page_size, skip=skip)
        batch = raw.get("value") or raw.get("Value") or []
        if not isinstance(batch, list) or not batch:
            return out
        out.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < page_size:
            return out
        skip += len(batch)


async def walk(
    client: _FolderLister,
    root_id: int,
    *,
    recursive: bool = True,
    max_entries: int = 100_000,
    page_size: int = 100,
    on_progress: Callable[[int], None] | None = None,
) -> tuple[list[ManifestRow], ManifestSummary]:
    """Traverse a folder tree breadth-first, returning every entry found.

    ``max_entries`` is a stop, not a filter: hitting it sets
    ``summary.truncated`` so the caller can say the walk was incomplete
    rather than reporting a partial count as if it were the total.

    A folder that fails to list is recorded in ``summary.folders_unreadable``
    and the walk continues — one permission-denied subtree shouldn't abort an
    audit of the other nine hundred.
    """
    rows: list[ManifestRow] = []
    summary = ManifestSummary()
    extensions: Counter[str] = Counter()
    templates: Counter[str] = Counter()

    # (folder_id, depth). Visited guards against a cycle; the repository
    # shouldn't contain one, but an infinite walk is an ugly way to find out.
    queue: list[tuple[int, int]] = [(root_id, 0)]
    visited: set[int] = {root_id}

    while queue:
        folder_id, depth = queue.pop(0)

        try:
            children = await _list_all_children(client, folder_id, page_size=page_size)
        except Exception:  # noqa: BLE001 — recorded below; one subtree can't abort the walk
            summary.folders_unreadable.append(folder_id)
            continue

        for raw in children:
            if len(rows) >= max_entries:
                summary.truncated = True
                queue.clear()
                break

            row = _row_from_api(raw, depth + 1)
            rows.append(row)
            summary.max_depth = max(summary.max_depth, row.depth)

            is_folder = row.entry_type.lower() == "folder"
            if is_folder:
                summary.folders += 1
                if recursive and row.entry_id not in visited:
                    visited.add(row.entry_id)
                    queue.append((row.entry_id, depth + 1))
            else:
                summary.documents += 1
                extensions[(row.extension or "(none)").lower()] += 1

            if row.template_name:
                templates[row.template_name] += 1

            if on_progress is not None and len(rows) % 100 == 0:
                on_progress(len(rows))

    summary.total = len(rows)
    summary.by_extension = dict(extensions.most_common())
    summary.by_template = dict(templates.most_common())
    return rows, summary


def write_csv(rows: list[ManifestRow], dest: Path) -> None:
    """Write rows as CSV with a stable column order.

    ``newline=""`` and ``utf-8-sig`` are both for Excel's benefit: without
    them Windows Excel doubles the line breaks and mangles accented names.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({column: getattr(row, column) for column in CSV_COLUMNS})


def write_jsonl(rows: list[ManifestRow], dest: Path) -> None:
    """Write one JSON object per line — the shape ``jq`` and pandas both like."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
