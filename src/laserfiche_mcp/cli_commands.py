"""Subcommand implementations for the ``laserfiche-mcp`` CLI.

Every command here does the same work the MCP tools do, with no model in the
loop and no tokens spent. The logic lives in ``ops/``; this module is the
thin layer that opens a client, calls it, and prints the result.

Two output conventions, both deliberate:

* Human-readable goes to **stdout**; progress and warnings go to **stderr**.
  That keeps ``laserfiche-mcp manifest ... | head`` and friends usable.
* ``--json`` switches stdout to a single machine-readable document, so the
  same command can back a shell script or a cron job.

Exit codes: 0 success, 1 an operation failed, 2 bad usage or config.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .auth import build_auth_strategy
from .client import LaserficheClient
from .config import Settings
from .errors import LaserficheError
from .models import EntrySummary
from .ops import compare, duplicates, extract, find, manifest
from .ops.content_search import build_search_command, run_search
from .ops.pages import parse_page_spec

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


# --- shared plumbing ---------------------------------------------------------


def warn(message: str) -> None:
    """Write a progress or diagnostic line to stderr, never stdout."""
    print(message, file=sys.stderr)


def emit_json(payload: Any) -> None:
    """Print one JSON document, converting dataclasses on the way out."""

    def default(obj: Any) -> Any:
        if is_dataclass(obj) and not isinstance(obj, type):
            return asdict(obj)
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        return str(obj)

    print(json.dumps(payload, indent=2, ensure_ascii=False, default=default))


@asynccontextmanager
async def open_client(settings: Settings) -> AsyncIterator[LaserficheClient]:
    """Open an authenticated client for the duration of one command."""
    auth = build_auth_strategy(settings)
    async with LaserficheClient(settings, auth) as client:
        yield client


async def resolve_entry_id(client: LaserficheClient, ref: str) -> int:
    """Accept either a numeric entry ID or a repository path.

    Paths are the way people actually refer to documents, and requiring an
    integer would mean every invocation starts with a lookup the tool could
    have done itself.
    """
    if ref.lstrip("-").isdigit():
        return int(ref)
    entry = await client.get_entry_by_path(ref)
    entry_id = entry.get("id") or entry.get("Id")
    if not isinstance(entry_id, int):
        raise LaserficheError(f"Path {ref!r} did not resolve to an entry with an ID.")
    return entry_id


def _entry_label(raw: dict[str, Any]) -> str:
    return str(raw.get("name") or raw.get("Name") or "(unnamed)")


def _filename_hint(raw: dict[str, Any], entry_id: int) -> str:
    """Best filename for format detection: entry name + `extension` attribute.

    Laserfiche entry names frequently omit the extension — it lives in the
    entry's ``extension`` attribute. Detection (and the scratch download
    name) lean on the suffix, so fold the attribute in when the name has
    none: "Contract 2024" + extension "pdf" -> "Contract 2024.pdf".
    """
    name = _entry_label(raw)
    extension = str(raw.get("extension") or raw.get("Extension") or "")
    if extension and not Path(name).suffix:
        return f"{name or entry_id}.{extension.lstrip('.')}"
    return name


@asynccontextmanager
async def downloaded(
    client: LaserficheClient,
    entry_id: int,
    filename: str,
    *,
    max_bytes: int | None = None,
) -> AsyncIterator[tuple[Path, str | None]]:
    """Stream an entry's edoc to a temp file. Yields ``(path, content_type)``.

    Streamed rather than buffered so a multi-hundred-megabyte scan costs one
    buffer, and cleaned up on the way out so a long-running shell session
    doesn't accumulate copies of the repository in the temp directory.
    """
    scratch = Path(tempfile.mkdtemp(prefix="lf-cli-"))
    # Keep the real extension: format detection leans on it far more than on
    # the content-type header, which self-hosted builds rarely set usefully.
    # Sanitized because entry names may contain characters (":", "?", ...)
    # that are invalid in local file names on Windows.
    target = scratch / extract.sanitize_filename(filename, fallback=f"{entry_id}.bin")
    try:
        _, content_type, _ = await client.export_entry_to_file(
            entry_id, target, max_bytes=max_bytes
        )
        yield target, content_type
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def human_bytes(count: int | None) -> str:
    if count is None:
        return "?"
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# --- commands ----------------------------------------------------------------


async def cmd_ls(client: LaserficheClient, args: argparse.Namespace) -> int:
    """List a folder's immediate children."""
    folder_id = await resolve_entry_id(client, args.folder)
    raw = await client.list_folder(
        folder_id, max_results=args.limit, skip=args.skip, include_count=True
    )
    entries = [EntrySummary.from_api(item) for item in raw.get("value", [])]

    if args.json:
        emit_json(
            {
                "folder_id": folder_id,
                "total_count": raw.get("@odata.count"),
                "returned": len(entries),
                "entries": [e.model_dump(mode="json") for e in entries],
            }
        )
        return EXIT_OK

    total = raw.get("@odata.count")
    for entry in entries:
        kind = "d" if str(entry.entry_type.value).lower() == "folder" else "-"
        print(f"{kind} {entry.id:>9}  {entry.name}")
    shown = f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
    print(f"\n{shown}" + (f" of {total}" if total is not None else ""))
    return EXIT_OK


async def cmd_get(client: LaserficheClient, args: argparse.Namespace) -> int:
    """Download an entry's electronic document to a local file."""
    entry_id = await resolve_entry_id(client, args.entry)
    entry = await client.get_entry(entry_id)
    name = _entry_label(entry)
    # Entry names may contain characters invalid in local file names; only
    # sanitized when WE pick the name — an explicit --to FILE is used as-is.
    safe_name = extract.sanitize_filename(name, fallback=f"{entry_id}.bin")

    dest = Path(args.to) if args.to else Path.cwd() / safe_name
    if dest.is_dir():
        dest = dest / safe_name
    if dest.exists() and not args.force:
        warn(f"Refusing to overwrite {dest} — pass --force to replace it.")
        return EXIT_USAGE

    written, content_type, digest = await client.export_entry_to_file(
        entry_id, dest, max_bytes=args.max_bytes
    )

    if args.json:
        emit_json(
            {
                "entry_id": entry_id,
                "path": str(dest),
                "byte_size": written,
                "content_type": content_type,
                "sha256": digest,
            }
        )
    else:
        print(f"{dest}  ({human_bytes(written)}, {content_type or 'unknown type'})")
        print(f"sha256  {digest}")
    return EXIT_OK


def _select_pages(extracted: extract.ExtractedText, spec: str | None) -> tuple[str, str | None]:
    """Apply a ``--pages`` selection. Returns ``(text, error_message)``."""
    if not spec:
        return extracted.text, None
    if extracted.pages is None:
        return "", "--pages only applies to paginated documents (PDF, PPTX)."

    indices, error = parse_page_spec(spec)
    if error is not None:
        return "", error
    if indices is None:
        return extracted.text, None

    available = [i for i in indices if i < len(extracted.pages)]
    if not available:
        return "", (
            f"None of the requested pages exist — the document has {len(extracted.pages)} page(s)."
        )
    return "\n".join(extracted.pages[i] for i in available), None


async def cmd_cat(client: LaserficheClient, args: argparse.Namespace) -> int:
    """Print an entry's extracted text to stdout."""
    entry_id = await resolve_entry_id(client, args.entry)
    entry = await client.get_entry(entry_id)
    name = _filename_hint(entry, entry_id)

    async with downloaded(client, entry_id, name, max_bytes=args.max_bytes) as (path, ctype):
        try:
            extracted = extract.extract(path, content_type=ctype, filename=name)
        except extract.ExtractionError as exc:
            warn(f"{exc.slug}: {exc.message}")
            return EXIT_FAILED

    text, error = _select_pages(extracted, args.pages)
    if error is not None:
        warn(error)
        return EXIT_USAGE

    if args.max_chars and len(text) > args.max_chars:
        text = text[: args.max_chars]
        extracted.warnings.append(f"Output truncated at {args.max_chars} characters.")

    if args.json:
        emit_json(
            {
                "entry_id": entry_id,
                "name": name,
                "backend": extracted.backend,
                "page_count": extracted.page_count,
                "char_count": len(text),
                "warnings": extracted.warnings,
                "text": text,
            }
        )
        return EXIT_OK

    for message in extracted.warnings:
        warn(f"warning: {message}")
    print(text)
    return EXIT_OK


async def cmd_find(client: LaserficheClient, args: argparse.Namespace) -> int:
    """Search inside one document and print matches with page numbers."""
    entry_id = await resolve_entry_id(client, args.entry)
    entry = await client.get_entry(entry_id)
    name = _filename_hint(entry, entry_id)

    async with downloaded(client, entry_id, name, max_bytes=args.max_bytes) as (path, ctype):
        try:
            extracted = extract.extract(path, content_type=ctype, filename=name)
        except extract.ExtractionError as exc:
            warn(f"{exc.slug}: {exc.message}")
            return EXIT_FAILED

    try:
        matches, total = find.find_in_text(
            extracted,
            args.pattern,
            regex=args.regex,
            ignore_case=not args.case_sensitive,
            context_chars=args.context,
            max_matches=args.limit,
        )
    except re.error as exc:
        warn(f"invalid regex: {exc}")
        return EXIT_USAGE

    if args.json:
        emit_json(
            {
                "entry_id": entry_id,
                "name": name,
                "pattern": args.pattern,
                "total_matches": total,
                "returned": len(matches),
                "matches": [asdict(m) for m in matches],
            }
        )
        return EXIT_OK

    for match in matches:
        where = f"p{match.page}:{match.line}" if match.page else f"line {match.line}"
        print(f"{where:>12}  {match.context}")

    if total == 0:
        warn(f"No match for {args.pattern!r} in {name}.")
        for message in extracted.warnings:
            warn(f"warning: {message}")
        return EXIT_FAILED

    suffix = f" (showing first {len(matches)})" if total > len(matches) else ""
    print(f"\n{total} match{'' if total == 1 else 'es'}{suffix}")
    return EXIT_OK


async def cmd_search(client: LaserficheClient, args: argparse.Namespace) -> int:
    """Full-text search across the repository, with matched passages."""
    command = build_search_command(args.query, args.folder)
    try:
        outcome = await run_search(
            client,
            command,
            page_size=args.limit,
            hits_for_top=args.hits_for_top,
            hits_per_entry=args.hits_per_entry,
            context_chars=args.context,
            timeout_seconds=args.timeout,
        )
    except LaserficheError as exc:
        if exc.status_code in (404, 405, 501):
            warn(
                "This Laserfiche build has no asynchronous /Searches endpoints, "
                "so matched passages are unavailable on it."
            )
            return EXIT_FAILED
        raise

    if outcome.failure is not None:
        warn(f"{outcome.failure['error']}: {outcome.failure.get('message', '')}".rstrip(": "))
        return EXIT_FAILED

    if args.json:
        emit_json(
            {
                "query": command,
                "total_count": outcome.total_count,
                "returned": len(outcome.results),
                "results": [r.model_dump(mode="json") for r in outcome.results],
            }
        )
        return EXIT_OK

    for result in outcome.results:
        print(f"{result.entry_id:>9}  {result.name}")
        for hit in result.hits:
            marker = f"p{hit.page}" if hit.page else (hit.field_name or "—")
            print(f"           {marker:>5}  {hit.text}")
        if result.hits_truncated and result.hit_count:
            print(f"           ...    {result.hit_count} matches in total")

    if not outcome.results:
        warn(f"No documents matched {command}.")
        return EXIT_FAILED

    print(f"\n{len(outcome.results)} document(s) matched")
    return EXIT_OK


async def cmd_manifest(client: LaserficheClient, args: argparse.Namespace) -> int:
    """Walk a folder tree and write an inventory to disk."""
    folder_id = await resolve_entry_id(client, args.folder)

    def progress(count: int) -> None:
        print(f"\r  walked {count} entries...", end="", file=sys.stderr, flush=True)

    rows, summary = await manifest.walk(
        client,
        folder_id,
        recursive=not args.no_recursive,
        max_entries=args.max_entries,
        on_progress=None if args.json else progress,
    )
    if not args.json:
        print("", file=sys.stderr)

    dest = Path(args.out) if args.out else None
    if dest is not None:
        if args.format == "jsonl":
            manifest.write_jsonl(rows, dest)
        else:
            manifest.write_csv(rows, dest)

    payload = {
        "folder_id": folder_id,
        "output": str(dest) if dest else None,
        **asdict(summary),
    }

    if args.json:
        emit_json(payload if dest else {**payload, "rows": [asdict(r) for r in rows]})
        return EXIT_OK

    print(f"Entries      {summary.total}")
    print(f"  folders    {summary.folders}")
    print(f"  documents  {summary.documents}")
    print(f"Max depth    {summary.max_depth}")
    if summary.by_extension:
        top = list(summary.by_extension.items())[:8]
        print("By extension " + ", ".join(f"{ext}={n}" for ext, n in top))
    if summary.by_template:
        top = list(summary.by_template.items())[:5]
        print("By template  " + ", ".join(f"{name}={n}" for name, n in top))
    if summary.folders_unreadable:
        warn(f"warning: {len(summary.folders_unreadable)} folder(s) could not be listed.")
    if summary.truncated:
        warn(f"warning: stopped at --max-entries ({args.max_entries}); tree is larger.")
    if dest is not None:
        print(f"\nWrote {dest}")
    else:
        warn("\nNo --out given, so nothing was written. Pass --out FILE to keep the rows.")
    return EXIT_OK


async def cmd_dedupe(client: LaserficheClient, args: argparse.Namespace) -> int:
    """Find byte-identical documents in a folder tree."""
    folder_id = await resolve_entry_id(client, args.folder)

    rows, _ = await manifest.walk(
        client, folder_id, recursive=not args.no_recursive, max_entries=args.max_entries
    )
    documents = [
        {"entry_id": r.entry_id, "name": r.name} for r in rows if r.entry_type.lower() != "folder"
    ]

    def progress(phase: str, done: int, total: int) -> None:
        print(f"\r  {phase} {done}/{total}...", end="", file=sys.stderr, flush=True)

    report = await duplicates.find_duplicates(
        client,
        documents,
        max_bytes=args.max_bytes,
        on_progress=None if args.json else progress,
    )
    if not args.json:
        print("", file=sys.stderr)

    if args.json:
        emit_json(
            {
                "folder_id": folder_id,
                "documents_examined": report.documents_examined,
                "documents_hashed": report.documents_hashed,
                "bytes_downloaded": report.bytes_downloaded,
                "total_wasted_bytes": report.total_wasted_bytes,
                "groups": [asdict(g) for g in report.groups],
                "skipped": report.skipped,
            }
        )
        return EXIT_OK

    for group in report.groups:
        print(f"{human_bytes(group.byte_size)}  x{len(group.entries)}  {group.sha256[:16]}")
        for entry in group.entries:
            print(f"    {entry['entry_id']:>9}  {entry['name']}")

    print(
        f"\n{len(report.groups)} duplicate group(s); "
        f"{human_bytes(report.total_wasted_bytes)} recoverable"
    )
    print(
        f"Examined {report.documents_examined} documents, "
        f"downloaded {report.documents_hashed} "
        f"({human_bytes(report.bytes_downloaded)}) — the rest were ruled out by size."
    )
    if report.skipped:
        warn(f"warning: {len(report.skipped)} document(s) skipped; see --json for reasons.")
    return EXIT_OK


async def cmd_diff(client: LaserficheClient, args: argparse.Namespace) -> int:
    """Compare two entries' metadata and template fields."""
    left_id = await resolve_entry_id(client, args.left)
    right_id = await resolve_entry_id(client, args.right)

    left_entry = await client.get_entry(left_id)
    right_entry = await client.get_entry(right_id)

    left_fields = right_fields = None
    if not args.no_fields:
        left_fields = await client.get_field_values(left_id)
        right_fields = await client.get_field_values(right_id)

    result = compare.compare_entries(
        left_entry,
        right_entry,
        left_fields=left_fields,
        right_fields=right_fields,
    )

    if args.json:
        emit_json(asdict(result))
        return EXIT_OK

    if result.identical:
        print(f"{left_id} and {right_id} match on every compared attribute and field.")
        return EXIT_OK

    for difference in result.differences:
        print(f"~ {difference.name} ({difference.kind})")
        print(f"    {left_id}: {difference.left!r}")
        print(f"    {right_id}: {difference.right!r}")
    for name in result.only_left_fields:
        print(f"- {name}  (only on {left_id})")
    for name in result.only_right_fields:
        print(f"+ {name}  (only on {right_id})")

    print(f"\n{len(result.differences)} difference(s), {len(result.same)} identical")
    return EXIT_FAILED if result.differences else EXIT_OK


# Office-friendly synonyms accepted by the parser. `ls`/`cat`/`diff` are
# muscle memory for developers; `list`/`read`/`compare` are what a records
# manager will guess first. Both spellings run the same function.
COMMAND_ALIASES = {
    "list": "ls",
    "download": "get",
    "read": "cat",
    "compare": "diff",
    "duplicates": "dedupe",
}

COMMANDS = {
    "ls": cmd_ls,
    "get": cmd_get,
    "cat": cmd_cat,
    "find": cmd_find,
    "search": cmd_search,
    "manifest": cmd_manifest,
    "dedupe": cmd_dedupe,
    "diff": cmd_diff,
}


async def _dispatch(settings: Settings, args: argparse.Namespace) -> int:
    handler = COMMANDS[COMMAND_ALIASES.get(args.command, args.command)]
    async with open_client(settings) as client:
        return await handler(client, args)


def run(settings: Settings, args: argparse.Namespace) -> int:
    """Run one subcommand. Returns the process exit code.

    ``LaserficheError`` is caught here and printed as one line — a stack
    trace is the wrong output for "that entry doesn't exist".
    """
    try:
        return asyncio.run(_dispatch(settings, args))
    except LaserficheError as exc:
        warn(f"laserfiche-mcp {args.command}: {_humanize_error(exc)}")
        return EXIT_FAILED
    except (OSError, ValueError) as exc:
        # A local filesystem problem (permission denied, disk full, bad
        # destination path) — one line, not a traceback. ValueError is
        # included because pathological Windows paths (e.g. CONIN$) raise
        # it instead of OSError from open().
        warn(f"laserfiche-mcp {args.command}: local file error — {exc}")
        return EXIT_FAILED
    except KeyboardInterrupt:
        warn("interrupted")
        return EXIT_FAILED


def _humanize_error(exc: LaserficheError) -> str:
    """One readable line instead of raw ProblemDetails JSON.

    ``str(exc)`` embeds the server's whole error body — JSON soup to the
    person at the terminal. Pull out the title and status, and translate
    the failures a non-developer can actually act on.
    """
    from .errors import lf_error_detail  # noqa: PLC0415

    if exc.status_code is None:
        return (
            "could not reach the Laserfiche server. Check your network/VPN, "
            "or re-run `laserfiche-mcp setup` to fix the server address."
        )

    detail = lf_error_detail(exc)
    title = detail.get("title") or detail.get("message")
    if exc.status_code in (401, 403):
        return (
            "the Laserfiche server rejected the login. Re-run "
            "`laserfiche-mcp setup`, or ask your administrator."
        )
    if exc.status_code == 404:
        return f"not found{f' — {title}' if title else ''}. Check the ID or path."
    if title:
        return f"HTTP {exc.status_code}: {title}"
    return f"HTTP {exc.status_code}: {exc}"


def command_names() -> Sequence[str]:
    return tuple(COMMANDS)
