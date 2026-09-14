"""Find byte-identical documents in a folder tree.

Duplicate detection is the archetypal job an LLM should never do: it is a
hash comparison, it has one correct answer, and the input is megabytes of
binary. Doing it here costs the user nothing and cannot be wrong.

The expensive part is downloading bytes, so the walk is two-pass:

1. Probe every document's size with a headers-only request (no body).
2. Download and hash **only** documents that share a size with another.

Files with a unique byte size cannot have a byte-identical twin, so pass 2
usually touches a small fraction of the tree. On a repository of mostly
distinct documents this turns "download everything" into "download almost
nothing".
"""

from __future__ import annotations

import shutil
import tempfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..errors import LaserficheError


class _HashClient(Protocol):
    """The two client methods this module needs."""

    async def export_entry_meta_only(
        self, entry_id: int, *, part: str = "Edoc"
    ) -> tuple[int | None, str | None]: ...

    async def export_entry_to_file(
        self, entry_id: int, dest: Path, *, part: str = "Edoc", max_bytes: int | None = None
    ) -> tuple[int, str | None, str]: ...


@dataclass
class DuplicateGroup:
    """A set of entries whose bytes are identical."""

    sha256: str
    byte_size: int
    entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def wasted_bytes(self) -> int:
        """Storage recoverable if all but one copy were removed."""
        return self.byte_size * max(0, len(self.entries) - 1)


@dataclass
class DuplicateReport:
    groups: list[DuplicateGroup] = field(default_factory=list)
    documents_examined: int = 0
    documents_hashed: int = 0
    """How many actually needed downloading — the rest were ruled out by size."""
    bytes_downloaded: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    """Documents that could not be sized or hashed, with the reason."""

    @property
    def total_wasted_bytes(self) -> int:
        return sum(group.wasted_bytes for group in self.groups)


async def find_duplicates(
    client: _HashClient,
    documents: list[dict[str, Any]],
    *,
    max_bytes: int | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> DuplicateReport:
    """Group ``documents`` by content hash.

    ``documents`` is a list of dicts carrying at least ``entry_id`` and
    ``name`` — normally ``ManifestRow`` output filtered to documents.

    ``max_bytes`` skips individual files larger than the limit rather than
    failing the run; they are listed in ``report.skipped`` so a large file is
    never silently treated as unique.
    """
    report = DuplicateReport(documents_examined=len(documents))

    # --- pass 1: size probe (headers only, no bodies transferred) ----------
    by_size: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, doc in enumerate(documents, start=1):
        entry_id = int(doc["entry_id"])
        if on_progress is not None:
            on_progress("size", index, len(documents))
        try:
            size, _ = await client.export_entry_meta_only(entry_id)
        except LaserficheError as exc:
            report.skipped.append(
                {"entry_id": entry_id, "name": doc.get("name"), "reason": f"size probe: {exc}"}
            )
            continue

        if size is None:
            # No Content-Length. Can't rule it out by size, so it must be
            # hashed; bucket it under a key nothing else will collide with.
            by_size[-1].append(doc)
            continue
        if max_bytes is not None and size > max_bytes:
            report.skipped.append(
                {
                    "entry_id": entry_id,
                    "name": doc.get("name"),
                    "reason": f"larger than max_bytes ({size} > {max_bytes})",
                }
            )
            continue
        by_size[size].append(doc)

    # A unique size means no possible byte-identical twin.
    candidates = [(size, docs) for size, docs in by_size.items() if len(docs) > 1 or size == -1]

    # --- pass 2: hash only the size collisions ------------------------------
    by_hash: dict[str, DuplicateGroup] = {}
    scratch = Path(tempfile.mkdtemp(prefix="lf-dedupe-"))
    total_to_hash = sum(len(docs) for _, docs in candidates)
    hashed = 0

    try:
        for _size, docs in candidates:
            for doc in docs:
                entry_id = int(doc["entry_id"])
                hashed += 1
                if on_progress is not None:
                    on_progress("hash", hashed, total_to_hash)

                target = scratch / f"{entry_id}.bin"
                try:
                    written, _, digest = await client.export_entry_to_file(
                        entry_id, target, max_bytes=max_bytes
                    )
                except LaserficheError as exc:
                    report.skipped.append(
                        {"entry_id": entry_id, "name": doc.get("name"), "reason": str(exc)}
                    )
                    continue
                finally:
                    target.unlink(missing_ok=True)

                report.documents_hashed += 1
                report.bytes_downloaded += written

                group = by_hash.get(digest)
                if group is None:
                    group = DuplicateGroup(sha256=digest, byte_size=written)
                    by_hash[digest] = group
                group.entries.append({"entry_id": entry_id, "name": doc.get("name")})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    report.groups = sorted(
        (g for g in by_hash.values() if len(g.entries) > 1),
        key=lambda g: g.wasted_bytes,
        reverse=True,
    )
    return report
