"""Tests for ``ops/duplicates.py``.

The behaviour that matters most here is the size prefilter: a document with a
unique byte size cannot have a byte-identical twin, so it must never be
downloaded. Several tests assert on *which* entries were fetched, not just on
the grouping, because getting that wrong turns a cheap audit into a
full-repository download.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from laserfiche_mcp.errors import LaserficheError
from laserfiche_mcp.ops import duplicates


class FakeDocs:
    """Serves sizes from a table and writes canned bytes on download."""

    def __init__(
        self,
        contents: dict[int, bytes],
        *,
        sizes: dict[int, int | None] | None = None,
        size_errors: set[int] | None = None,
        download_errors: set[int] | None = None,
    ) -> None:
        self.contents = contents
        self.sizes = sizes if sizes is not None else {k: len(v) for k, v in contents.items()}
        self.size_errors = size_errors or set()
        self.download_errors = download_errors or set()
        self.downloaded: list[int] = []

    async def export_entry_meta_only(
        self, entry_id: int, *, part: str = "Edoc"
    ) -> tuple[int | None, str | None]:
        if entry_id in self.size_errors:
            raise LaserficheError("probe failed", status_code=500)
        return self.sizes.get(entry_id), "application/octet-stream"

    async def export_entry_to_file(
        self, entry_id: int, dest: Path, *, part: str = "Edoc", max_bytes: int | None = None
    ) -> tuple[int, str | None, str]:
        if entry_id in self.download_errors:
            raise LaserficheError("download failed", status_code=500)
        self.downloaded.append(entry_id)
        body = self.contents[entry_id]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return len(body), "application/octet-stream", hashlib.sha256(body).hexdigest()


def _docs(*ids: int) -> list[dict[str, Any]]:
    return [{"entry_id": i, "name": f"doc{i}.pdf"} for i in ids]


@pytest.mark.asyncio
async def test_identical_documents_are_grouped() -> None:
    client = FakeDocs({1: b"same bytes", 2: b"same bytes", 3: b"different!"})

    report = await duplicates.find_duplicates(client, _docs(1, 2, 3))

    assert len(report.groups) == 1
    assert {e["entry_id"] for e in report.groups[0].entries} == {1, 2}


@pytest.mark.asyncio
async def test_unique_sizes_are_never_downloaded() -> None:
    """The whole point of the two-pass design."""
    client = FakeDocs({1: b"aa", 2: b"bbbb", 3: b"cccccc"})

    report = await duplicates.find_duplicates(client, _docs(1, 2, 3))

    assert client.downloaded == []
    assert report.documents_hashed == 0
    assert report.groups == []


@pytest.mark.asyncio
async def test_same_size_different_bytes_is_not_a_duplicate() -> None:
    """Size collision forces a hash, which then rules the pair out."""
    client = FakeDocs({1: b"aaaa", 2: b"bbbb"})

    report = await duplicates.find_duplicates(client, _docs(1, 2))

    assert sorted(client.downloaded) == [1, 2]
    assert report.groups == []


@pytest.mark.asyncio
async def test_wasted_bytes_counts_all_but_one_copy() -> None:
    client = FakeDocs({1: b"x" * 100, 2: b"x" * 100, 3: b"x" * 100})

    report = await duplicates.find_duplicates(client, _docs(1, 2, 3))

    assert report.groups[0].wasted_bytes == 200
    assert report.total_wasted_bytes == 200


@pytest.mark.asyncio
async def test_documents_without_content_length_are_still_hashed() -> None:
    """No declared size means it can't be ruled out, so it must be checked."""
    client = FakeDocs({1: b"same", 2: b"same"}, sizes={1: None, 2: None})

    report = await duplicates.find_duplicates(client, _docs(1, 2))

    assert sorted(client.downloaded) == [1, 2]
    assert len(report.groups) == 1


@pytest.mark.asyncio
async def test_oversized_documents_are_skipped_with_a_reason() -> None:
    """Silently treating a large file as unique would be a wrong answer."""
    client = FakeDocs({1: b"x" * 10, 2: b"x" * 10, 3: b"y" * 5000})

    report = await duplicates.find_duplicates(client, _docs(1, 2, 3), max_bytes=1000)

    assert 3 not in client.downloaded
    assert any(s["entry_id"] == 3 and "max_bytes" in s["reason"] for s in report.skipped)


@pytest.mark.asyncio
async def test_a_failed_size_probe_is_recorded_not_raised() -> None:
    client = FakeDocs({1: b"aa", 2: b"aa"}, size_errors={2})

    report = await duplicates.find_duplicates(client, _docs(1, 2))

    assert [s["entry_id"] for s in report.skipped] == [2]


@pytest.mark.asyncio
async def test_a_failed_download_is_recorded_not_raised() -> None:
    client = FakeDocs({1: b"same", 2: b"same"}, download_errors={2})

    report = await duplicates.find_duplicates(client, _docs(1, 2))

    assert [s["entry_id"] for s in report.skipped] == [2]
    assert report.groups == []  # only one copy survived, so no group


@pytest.mark.asyncio
async def test_groups_are_ordered_by_recoverable_space() -> None:
    client = FakeDocs(
        {
            1: b"s" * 10,
            2: b"s" * 10,
            3: b"L" * 1000,
            4: b"L" * 1000,
        }
    )

    report = await duplicates.find_duplicates(client, _docs(1, 2, 3, 4))

    assert [g.byte_size for g in report.groups] == [1000, 10]


@pytest.mark.asyncio
async def test_scratch_files_are_cleaned_up(tmp_path: Path) -> None:
    """A dedupe run must not leave a copy of the repository in temp."""
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("lf-dedupe-*"))
    client = FakeDocs({1: b"same", 2: b"same"})

    await duplicates.find_duplicates(client, _docs(1, 2))

    after = set(Path(tempfile.gettempdir()).glob("lf-dedupe-*"))
    assert after == before
