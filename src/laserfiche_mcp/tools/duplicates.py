"""``find_duplicate_documents`` — byte-identical document detection.

This is the MCP wrapper. The walk-then-hash orchestration lives in
``ops/manifest.py`` (tree walk) and ``ops/duplicates.py`` (size-then-hash
dedup) so the ``laserfiche-mcp dedupe`` subcommand runs exactly the same
flow without an LLM in the loop.

Duplicate detection is the archetypal job a model should never do by eye:
it is a hash comparison with one correct answer, over an input that is
megabytes of binary. Doing it here costs the user nothing and cannot be
wrong.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .. import _app
from .._app import get_settings
from ..errors import LaserficheError, classify_lf_error, local_error
from ..ops import duplicates as duplicates_ops
from ..ops import manifest
from ._helpers import entry_type
from ._registry import register

__all__ = ["find_duplicate_documents"]

# A blocking MCP call has no way to stream interim progress back to the
# model (unlike the CLI, which prints "size 400/1200..." to stderr), so an
# unbounded walk just hangs the tool call. 2000 keeps a single call fast
# enough for a demo/interactive session; raise it explicitly for a real
# audit of a larger tree.
DEFAULT_MAX_ENTRIES = 2_000


@register(v2_name="laserfiche_document_find_duplicates")
async def find_duplicate_documents(
    folder_id: Annotated[
        int,
        Field(
            description=(
                "Integer entry ID of the folder to scan. Resolve a path "
                "first with get_entry_by_path if you only have a location."
            ),
            ge=1,
        ),
    ],
    recursive: Annotated[
        bool,
        Field(
            default=True,
            description="Scan subfolders too. False = only this folder's immediate children.",
        ),
    ] = True,
    max_entries: Annotated[
        int,
        Field(
            default=DEFAULT_MAX_ENTRIES,
            description=(
                "Stop walking the tree after this many entries (folders + "
                "documents). A stop, not a filter — hitting it sets "
                "walk_truncated=true rather than silently reporting a "
                "partial tree as complete. Defaults to 2000; raise for an "
                "exhaustive audit of a larger tree, but expect the call to "
                "take proportionally longer since it runs to completion in "
                "one request."
            ),
            ge=1,
            le=100_000,
        ),
    ] = DEFAULT_MAX_ENTRIES,
    max_bytes: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Skip (don't download or hash) any document larger than "
                "this many bytes; it's listed in `skipped` with a reason "
                "rather than silently treated as unique. Defaults to "
                "LF_EDOC_MAX_BYTES (25 MB)."
            ),
            ge=1,
        ),
    ] = None,
) -> dict[str, Any]:
    """Find byte-identical documents in a folder tree and group them.

    **Use this to answer "are there duplicate files in here?" or "how much
    space would deduping this folder recover?"** — it downloads nothing for
    documents whose size doesn't collide with another's, and only hashes the
    ones that do, so it's usually far cheaper than it sounds. This is a
    read-only scan: it finds duplicates, it does not delete or merge them —
    follow up with ``delete_entry``/``delete_edoc`` yourself on whichever
    copies you decide to remove.

    Two-pass approach: first probes every document's size (headers only, no
    bytes transferred); only documents that share a size with another are
    then downloaded and hashed (SHA-256). A repository of mostly-distinct
    documents therefore touches a small fraction of the tree on pass two.

    This is a single blocking call with no interim progress — for a large
    ``max_entries`` this can take a while (network round-trips per document
    plus the downloads pass two triggers). Start with the default and raise
    ``max_entries`` only once you've seen how large the tree is, e.g. via
    ``list_folder``.

    Sibling tools: ``get_entry_by_path`` to resolve a path to the
    ``folder_id`` this tool needs; ``get_document_edoc`` to download or read
    a specific document once you've identified which copy to keep;
    ``compare_entries`` to check whether two SIMILAR-but-not-identical
    documents differ only in metadata.

    Returns ``{"mode": "duplicate_report", "folder_id", "recursive",
    "walk_truncated": bool, "documents_examined", "documents_hashed",
    "bytes_downloaded", "total_wasted_bytes", "max_bytes",
    "groups": [{"sha256", "byte_size", "wasted_bytes",
    "entries": [{"entry_id", "name"}, ...]}, ...],
    "skipped": [{"entry_id", "name", "reason"}, ...],
    "folders_unreadable": [<folder_id>, ...]}``. ``groups`` is sorted by
    ``wasted_bytes`` descending (biggest recoverable space first).
    ``folders_unreadable`` lists subfolders the walk couldn't list (usually
    permissions) — entries under them are NOT included, so a non-empty list
    means the scan was partial even if ``walk_truncated`` is false. On
    failure returns ``{"mode": "error", "error": <slug>, "entry_id":
    <folder_id>}`` — ``not_found``/``auth_failed`` (bad ``folder_id``) or
    ``not_a_folder`` (``folder_id`` points at a document).
    """
    settings = get_settings()
    client = _app.get_client()

    try:
        root_entry = await client.get_entry(folder_id)
    except LaserficheError as exc:
        return classify_lf_error("find_duplicate_documents", exc, entry_id=folder_id)

    if entry_type(root_entry).lower() != "folder":
        return local_error(
            "find_duplicate_documents",
            "not_a_folder",
            entry_id=folder_id,
            reason=f"entry {folder_id} is a {entry_type(root_entry) or 'document'}, not a folder",
        )

    rows, summary = await manifest.walk(
        client, folder_id, recursive=recursive, max_entries=max_entries
    )
    documents = [
        {"entry_id": row.entry_id, "name": row.name}
        for row in rows
        if row.entry_type.lower() != "folder"
    ]

    effective_max_bytes = max_bytes if max_bytes is not None else settings.edoc_max_bytes

    report = await duplicates_ops.find_duplicates(
        client,
        documents,
        max_bytes=effective_max_bytes,
    )

    return {
        "mode": "duplicate_report",
        "folder_id": folder_id,
        "recursive": recursive,
        "walk_truncated": summary.truncated,
        "documents_examined": report.documents_examined,
        "documents_hashed": report.documents_hashed,
        "bytes_downloaded": report.bytes_downloaded,
        "total_wasted_bytes": report.total_wasted_bytes,
        "max_bytes": effective_max_bytes,
        "groups": [
            {
                "sha256": group.sha256,
                "byte_size": group.byte_size,
                "wasted_bytes": group.wasted_bytes,
                "entries": group.entries,
            }
            for group in report.groups
        ],
        "skipped": report.skipped,
        "folders_unreadable": summary.folders_unreadable,
    }
