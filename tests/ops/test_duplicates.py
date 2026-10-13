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
async def test_a_local_io_error_during_download_is_recorded_not_raised() -> None:
    """export_entry_to_file streams to local disk (scratch dir mkdir/open/
    write); a local OSError (AV lock, full/locked temp dir) there is not
    a LaserficheError and must not abort the whole scan — one document's
    local I/O failure shouldn't cost the rest of the report."""

    class RaisesOSError(FakeDocs):
        async def export_entry_to_file(
            self, entry_id: int, dest: Path, *, part: str = "Edoc", max_bytes: int | None = None
        ) -> tuple[int, str | None, str]:
            if entry_id == 2:
                raise PermissionError("Access is denied")
            return await super().export_entry_to_file(
                entry_id, dest, part=part, max_bytes=max_bytes
            )

    client = RaisesOSError({1: b"same", 2: b"same"})

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


# --- entries with no electronic document ---------------------------------------
#
# A scanned entry (page images only) has no file. v2 rejects the export with a
# 400 / errorCode 216; v1 answers with an empty 200. Either way there are no
# bytes to compare, so the entry must be skipped with a reason — never grouped
# with other scanned entries as "duplicates", and never silently treated as
# unique.

_NO_EDOC_BODY = {
    "title": "This entry does not contain an electronic document",
    "status": 400,
    "errorCode": 216,
}


def _no_edoc_error() -> LaserficheError:
    return LaserficheError("HTTP 400", status_code=400, detail=_NO_EDOC_BODY)


class NoEdocDocs(FakeDocs):
    """FakeDocs where chosen entries raise the v2 'no electronic document' 400."""

    def __init__(
        self,
        contents: dict[int, bytes],
        *,
        probe_no_edoc: set[int] | None = None,
        download_no_edoc: set[int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__({k: v for k, v in contents.items()}, **kwargs)
        self.probe_no_edoc = probe_no_edoc or set()
        self.download_no_edoc = download_no_edoc or set()

    async def export_entry_meta_only(
        self, entry_id: int, *, part: str = "Edoc"
    ) -> tuple[int | None, str | None]:
        if entry_id in self.probe_no_edoc:
            raise _no_edoc_error()
        return await super().export_entry_meta_only(entry_id, part=part)

    async def export_entry_to_file(
        self, entry_id: int, dest: Path, *, part: str = "Edoc", max_bytes: int | None = None
    ) -> tuple[int, str | None, str]:
        if entry_id in self.download_no_edoc:
            raise _no_edoc_error()
        return await super().export_entry_to_file(entry_id, dest, part=part, max_bytes=max_bytes)


@pytest.mark.asyncio
async def test_v2_no_edoc_rejection_at_probe_is_skipped_with_a_reason() -> None:
    client = NoEdocDocs({1: b"same", 2: b"same", 3: b""}, probe_no_edoc={3})

    report = await duplicates.find_duplicates(client, _docs(1, 2, 3))

    assert [e["entry_id"] for g in report.groups for e in g.entries] == [1, 2]
    assert [s["entry_id"] for s in report.skipped] == [3]
    assert "no electronic document" in report.skipped[0]["reason"]
    # The reason is the friendly one, not the raw HTTP error.
    assert "HTTP 400" not in report.skipped[0]["reason"]


@pytest.mark.asyncio
async def test_two_scanned_entries_are_not_grouped_as_duplicates() -> None:
    """The v1 failure mode: an empty 200 gives size 0 for every scanned entry,
    which used to make them all look like identical zero-byte duplicates."""
    client = FakeDocs({1: b"", 2: b"", 3: b"real"})

    report = await duplicates.find_duplicates(client, _docs(1, 2, 3))

    assert report.groups == []
    assert sorted(s["entry_id"] for s in report.skipped) == [1, 2]
    assert all("no electronic document" in s["reason"] for s in report.skipped)
    assert client.downloaded == []


@pytest.mark.asyncio
async def test_v2_no_edoc_rejection_at_download_is_skipped_with_a_reason() -> None:
    # Size is unknown (no Content-Length), so the doc reaches pass 2, where the
    # server rejects the download.
    client = NoEdocDocs(
        {1: b"same", 2: b"same", 3: b""},
        sizes={1: None, 2: None, 3: None},
        download_no_edoc={3},
    )

    report = await duplicates.find_duplicates(client, _docs(1, 2, 3))

    assert len(report.groups) == 1
    assert [s["entry_id"] for s in report.skipped] == [3]
    assert "no electronic document" in report.skipped[0]["reason"]


@pytest.mark.asyncio
async def test_unknown_size_with_an_empty_body_is_skipped_not_hashed() -> None:
    client = FakeDocs({1: b"", 2: b""}, sizes={1: None, 2: None})

    report = await duplicates.find_duplicates(client, _docs(1, 2))

    assert report.groups == []
    assert report.documents_hashed == 0
    assert sorted(s["entry_id"] for s in report.skipped) == [1, 2]


@pytest.mark.asyncio
async def test_an_unrelated_400_keeps_the_raw_probe_reason() -> None:
    """Only the no-electronic-document 400 is reworded; other failures still say why."""

    class Rejecting(FakeDocs):
        async def export_entry_meta_only(
            self, entry_id: int, *, part: str = "Edoc"
        ) -> tuple[int | None, str | None]:
            raise LaserficheError(
                "HTTP 400",
                status_code=400,
                detail={"title": "Invalid parameter", "errorCode": 216},
            )

    report = await duplicates.find_duplicates(Rejecting({1: b"a"}), _docs(1))

    assert report.skipped[0]["reason"].startswith("size probe:")
