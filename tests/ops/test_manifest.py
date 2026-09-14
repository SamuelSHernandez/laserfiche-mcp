"""Tests for ``ops/manifest.py`` — the folder walk and its writers.

The walk is exercised against a dict-backed fake rather than httpx_mock: the
behaviour under test is traversal (paging, recursion, cycle safety, partial
failure), and a fake tree states those cases far more legibly than a stack of
mocked HTTP responses.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from laserfiche_mcp.ops import manifest


class FakeRepo:
    """Minimal ``list_folder`` over an in-memory tree."""

    def __init__(
        self, tree: dict[int, list[dict[str, Any]]], *, unreadable: set[int] | None = None
    ):
        self.tree = tree
        self.unreadable = unreadable or set()
        self.calls: list[tuple[int, int, int]] = []

    async def list_folder(
        self, folder_id: int, *, max_results: int, skip: int, include_count: bool = False
    ) -> dict[str, Any]:
        self.calls.append((folder_id, max_results, skip))
        if folder_id in self.unreadable:
            raise PermissionError("access denied")
        children = self.tree.get(folder_id, [])
        return {"value": children[skip : skip + max_results]}


def _doc(entry_id: int, name: str, **extra: Any) -> dict[str, Any]:
    return {"id": entry_id, "name": name, "entryType": "Document", **extra}


def _folder(entry_id: int, name: str) -> dict[str, Any]:
    return {"id": entry_id, "name": name, "entryType": "Folder"}


@pytest.mark.asyncio
async def test_walk_recurses_and_counts_by_type() -> None:
    repo = FakeRepo(
        {
            1: [_folder(2, "HR"), _doc(3, "a.pdf", extension="pdf")],
            2: [_doc(4, "b.docx", extension="docx"), _doc(5, "c.pdf", extension="pdf")],
        }
    )

    rows, summary = await manifest.walk(repo, 1)

    assert summary.total == 4
    assert summary.folders == 1
    assert summary.documents == 3
    assert summary.by_extension == {"pdf": 2, "docx": 1}
    assert {r.entry_id for r in rows} == {2, 3, 4, 5}


@pytest.mark.asyncio
async def test_walk_records_depth_relative_to_the_root() -> None:
    repo = FakeRepo({1: [_folder(2, "a")], 2: [_folder(3, "b")], 3: [_doc(4, "deep.pdf")]})

    rows, summary = await manifest.walk(repo, 1)

    depths = {r.entry_id: r.depth for r in rows}
    assert depths == {2: 1, 3: 2, 4: 3}
    assert summary.max_depth == 3


@pytest.mark.asyncio
async def test_no_recursive_stops_at_immediate_children() -> None:
    repo = FakeRepo({1: [_folder(2, "HR")], 2: [_doc(3, "hidden.pdf")]})

    rows, summary = await manifest.walk(repo, 1, recursive=False)

    assert [r.entry_id for r in rows] == [2]
    assert summary.total == 1


@pytest.mark.asyncio
async def test_walk_pages_through_a_large_folder() -> None:
    children = [_doc(i, f"{i}.pdf") for i in range(100, 250)]
    repo = FakeRepo({1: children})

    rows, summary = await manifest.walk(repo, 1, page_size=50)

    assert summary.total == 150
    # 150 divides evenly into pages of 50, so the third page comes back full
    # and a fourth request is what proves the folder is exhausted.
    assert [call[2] for call in repo.calls] == [0, 50, 100, 150]


@pytest.mark.asyncio
async def test_max_entries_marks_the_result_truncated() -> None:
    """A partial count reported as a total would be worse than no count."""
    repo = FakeRepo({1: [_doc(i, f"{i}.pdf") for i in range(50)]})

    rows, summary = await manifest.walk(repo, 1, max_entries=10)

    assert len(rows) == 10
    assert summary.truncated is True


@pytest.mark.asyncio
async def test_unreadable_folder_is_recorded_and_the_walk_continues() -> None:
    repo = FakeRepo(
        {1: [_folder(2, "locked"), _folder(3, "open")], 3: [_doc(4, "reachable.pdf")]},
        unreadable={2},
    )

    rows, summary = await manifest.walk(repo, 1)

    assert summary.folders_unreadable == [2]
    assert 4 in {r.entry_id for r in rows}


@pytest.mark.asyncio
async def test_walk_survives_a_cycle() -> None:
    """A folder that contains its own ancestor must not loop forever."""
    repo = FakeRepo({1: [_folder(2, "child")], 2: [_folder(1, "back to root")]})

    rows, summary = await manifest.walk(repo, 1)

    assert summary.total <= 2


@pytest.mark.asyncio
async def test_templates_are_counted_across_types() -> None:
    repo = FakeRepo(
        {1: [_doc(2, "a.pdf", templateName="Invoice"), _doc(3, "b.pdf", templateName="Invoice")]}
    )

    _, summary = await manifest.walk(repo, 1)

    assert summary.by_template == {"Invoice": 2}


# --- writers ----------------------------------------------------------------


def _rows() -> list[manifest.ManifestRow]:
    return [
        manifest.ManifestRow(
            entry_id=7, name="lease.pdf", entry_type="Document", parent_id=1, depth=1
        ),
        manifest.ManifestRow(entry_id=8, name="HR", entry_type="Folder", parent_id=1, depth=1),
    ]


def test_write_csv_uses_the_declared_column_order(tmp_path: Path) -> None:
    dest = tmp_path / "out.csv"

    manifest.write_csv(_rows(), dest)

    with dest.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        assert next(reader) == list(manifest.CSV_COLUMNS)
        assert next(reader)[:2] == ["7", "lease.pdf"]


def test_write_csv_creates_missing_parent_directories(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "deeper" / "out.csv"

    manifest.write_csv(_rows(), dest)

    assert dest.exists()


def test_write_jsonl_emits_one_object_per_line(tmp_path: Path) -> None:
    dest = tmp_path / "out.jsonl"

    manifest.write_jsonl(_rows(), dest)

    lines = dest.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["entry_id"] == 7
