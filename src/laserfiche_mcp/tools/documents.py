"""Tools for reading the bytes / extracted text of an electronic document."""

from __future__ import annotations

import base64
import io
import shutil
import tempfile
from pathlib import Path as _Path
from typing import Annotated, Any, Literal

from pydantic import Field

from .. import _app
from .._app import get_settings
from ..errors import LaserficheError, classify_lf_error, kind_for_subkind
from ..observability import get_request_id_or_new
from ..ops.pages import parse_page_spec
from ._registry import register

__all__ = ["get_document_edoc", "get_document_text", "parse_page_spec"]


@register(v2_name="laserfiche_document_get_text")
async def get_document_text(
    entry_id: Annotated[int, Field(description="Entry ID of an electronic document.", ge=1)],
    max_chars: Annotated[
        int,
        Field(
            default=50_000,
            description="Truncate the returned text after this many characters.",
            ge=1,
        ),
    ] = 50_000,
) -> dict[str, Any]:
    """Download a document's server-extracted text (v2 servers only).

    Use for reading a document's contents: the text comes from Laserfiche's
    own extraction pipeline (OCR for scans, upstream extraction for office
    files). v1 servers have no endpoint for this — there, use
    ``get_document_edoc(mode="text")`` instead.

    Returns ``{"entry_id", "text", "char_count", "truncated"}``. On failure
    returns ``{"mode": "error", "error": <slug>}`` (``not_found`` = folder or
    no extracted text; ``method_not_allowed``/``server_error`` = v1 server).
    """
    try:
        content = await _app.get_client().export_entry(entry_id, part="Text")
    except LaserficheError as exc:
        return classify_lf_error("get_document_text", exc, entry_id=entry_id)

    text = content.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {
        "entry_id": entry_id,
        "text": text,
        "char_count": len(text),
        "truncated": truncated,
    }


def _extract_pdf_text(
    content: bytes,
    char_limit: int,
    *,
    page_spec: list[int] | None = None,
    char_offset: int = 0,
) -> dict[str, Any]:
    """Run pypdf over a PDF byte string.

    Returns a result dict on success or an error dict on extraction failure
    (encrypted PDF, malformed PDF, pypdf-internal exception). The caller
    decides how to wrap this into the tool response.
    """
    try:
        import pypdf  # imported lazily so users on v2 don't need to install it
    except ImportError as exc:
        return {
            "error": "pypdf_unavailable",
            "message": (
                "pypdf is required for mode='text' on PDF documents. "
                "Install with `pip install pypdf` or `uv add pypdf`."
            ),
            "exception": repr(exc),
        }

    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001 — pypdf raises various subclasses
        return {
            "error": "pdf_open_failed",
            "exception_class": type(exc).__name__,
            "message": str(exc),
        }

    if reader.is_encrypted:
        return {
            "error": "pdf_encrypted",
            "message": (
                "PDF is password-protected; text extraction is not possible. "
                "Use mode='bytes' if you need the raw file."
            ),
        }

    pages_total = len(reader.pages)

    if page_spec is None:
        selected = list(range(pages_total))
        out_of_range: list[int] = []
    else:
        selected = [i for i in page_spec if i < pages_total]
        out_of_range = [i + 1 for i in page_spec if i >= pages_total]
        if not selected:
            return {
                "error": "pages_out_of_range",
                "message": (
                    f"None of the requested pages exist — this document has "
                    f"{pages_total} page(s), and {out_of_range} were requested."
                ),
            }

    chunks: list[str] = []
    pages_extracted = 0
    for index in selected:
        try:
            chunks.append(reader.pages[index].extract_text() or "")
            pages_extracted += 1
        except Exception as exc:  # noqa: BLE001 — partial extraction is acceptable
            chunks.append(f"[page extraction failed: {type(exc).__name__}]")

    full = "\n".join(chunks)
    total_chars = len(full)

    # char_offset windows into the *selected* pages, so it composes with
    # `pages` rather than fighting it.
    windowed = full[char_offset:] if char_offset else full
    truncated = len(windowed) > char_limit
    if truncated:
        windowed = windowed[:char_limit]

    next_offset = char_offset + len(windowed) if truncated else None

    result: dict[str, Any] = {
        "ok": True,
        "text": windowed,
        "pages_total": pages_total,
        "pages_extracted": pages_extracted,
        "truncated": truncated,
        "char_offset": char_offset,
        "chars_available": total_chars,
        "next_char_offset": next_offset,
    }
    if page_spec is not None:
        result["pages_selected"] = [i + 1 for i in selected]
    if out_of_range:
        result["pages_out_of_range"] = out_of_range
    return result


def _edoc_error(
    entry_id: int,
    requested_mode: str,
    subkind: str,
    **fields: Any,
) -> dict[str, Any]:
    """Failure payload for get_document_edoc, per the error contract.

    ``mode`` is always the literal ``"error"`` on failures — callers (and
    ``tool_logger``) branch on it — and the mode the caller asked for is
    preserved as ``requested_mode``.
    """
    return {
        "mode": "error",
        "operation": "get_document_edoc",
        "kind": kind_for_subkind(subkind),
        "error": subkind,
        "request_id": get_request_id_or_new(),
        "entry_id": entry_id,
        "requested_mode": requested_mode,
        **fields,
    }


def _edoc_info_response(
    entry_id: int,
    byte_size: int | None,
    content_type: str | None,
) -> dict[str, Any]:
    """``mode='info'`` payload — metadata only, nothing downloaded."""
    return {
        "entry_id": entry_id,
        "mode": "info",
        "byte_size": byte_size,
        "content_type": content_type,
        "hint": (
            "Headers only — the document body was never transferred. "
            "Use mode='text' for extracted text (prefer this: it is far "
            "cheaper in context than the raw file, and supports `pages` "
            "and `char_offset` so you can read part of a long document). "
            "mode='bytes' returns base64, which is expensive and usually "
            "not what you want."
            + (
                ""
                if byte_size is not None
                else " byte_size is null because the server answered without "
                "a Content-Length header."
            )
        ),
    }


def _edoc_size_cap_response(
    entry_id: int,
    mode: str,
    byte_size: int,
    effective_cap: int,
    content_type: str | None,
) -> dict[str, Any]:
    """Refused-by-size response shared by ``mode='bytes'`` and ``mode='text'``."""
    return _edoc_error(
        entry_id,
        mode,
        "size_exceeds_cap",
        byte_size=byte_size,
        max_bytes=effective_cap,
        content_type=content_type,
        message=(
            f"Edoc is {byte_size} bytes, which exceeds the {effective_cap}-byte cap. "
            "Pass max_bytes=<larger value> or raise LF_EDOC_MAX_BYTES "
            "if you really need this document."
        ),
    )


def _edoc_bytes_response(
    entry_id: int,
    content: bytes,
    byte_size: int,
    content_type: str | None,
) -> dict[str, Any]:
    return {
        "entry_id": entry_id,
        "mode": "bytes",
        "byte_size": byte_size,
        "content_type": content_type,
        "data_base64": base64.b64encode(content).decode("ascii"),
    }


def _edoc_text_response(
    entry_id: int,
    content: bytes,
    byte_size: int,
    content_type: str | None,
    text_char_limit: int,
    *,
    page_spec: list[int] | None = None,
    char_offset: int = 0,
) -> dict[str, Any]:
    """Extract text from the edoc based on content-type."""
    ct_lower = (content_type or "").lower().split(";")[0].strip()

    if ct_lower == "application/pdf":
        result = _extract_pdf_text(
            content,
            text_char_limit,
            page_spec=page_spec,
            char_offset=char_offset,
        )
        base = {
            "entry_id": entry_id,
            "mode": "text",
            "content_type": content_type,
            "byte_size": byte_size,
        }
        if result.get("ok"):
            return {**base, **{k: v for k, v in result.items() if k != "ok"}}
        return _edoc_error(
            entry_id,
            "text",
            result.get("error", "pdf_extraction_failed"),
            content_type=content_type,
            byte_size=byte_size,
            message=result.get("message"),
            exception_class=result.get("exception_class"),
            hint="Try mode='bytes' to retrieve the raw PDF for client-side handling.",
        )

    if ct_lower.startswith("text/"):
        if page_spec is not None:
            return _edoc_error(
                entry_id,
                "text",
                "pages_not_applicable",
                content_type=content_type,
                byte_size=byte_size,
                message=(
                    f"`pages` only applies to paginated documents; this entry is "
                    f"{content_type!r}. Use `char_offset` to window into it instead."
                ),
            )
        text = content.decode("utf-8", errors="replace")
        total_chars = len(text)
        windowed = text[char_offset:] if char_offset else text
        truncated = len(windowed) > text_char_limit
        if truncated:
            windowed = windowed[:text_char_limit]
        return {
            "entry_id": entry_id,
            "mode": "text",
            "content_type": content_type,
            "byte_size": byte_size,
            "text": windowed,
            "truncated": truncated,
            "char_offset": char_offset,
            "chars_available": total_chars,
            "next_char_offset": char_offset + len(windowed) if truncated else None,
        }

    raise AssertionError(
        "unreachable: non-PDF, non-plain-text content (including text/html "
        "and text/rtf) routes through _edoc_extract_via_ops"
    )  # pragma: no cover


async def _edoc_extract_via_ops(
    client: Any,
    entry_id: int,
    content: bytes,
    byte_size: int,
    content_type: str | None,
    text_char_limit: int,
    *,
    page_spec: list[int] | None,
    char_offset: int,
) -> dict[str, Any]:
    """Extract text from a non-PDF, non-text edoc via ``ops/extract``.

    Covers the office formats a real repository is full of — DOCX, PPTX,
    XLSX, EML, HTML, RTF — using the same extractors the CLI's ``cat``
    command uses. The entry name is fetched for extension-based format
    detection, because self-hosted servers label most edocs
    ``application/octet-stream``.
    """
    from ..ops import extract as ops_extract  # noqa: PLC0415 — optional-heavy import

    base: dict[str, Any] = {
        "entry_id": entry_id,
        "mode": "text",
        "content_type": content_type,
        "byte_size": byte_size,
    }

    name = ""
    extension = ""
    try:
        entry = await client.get_entry(entry_id)
        name = entry.get("name") or entry.get("Name") or ""
        extension = str(entry.get("extension") or entry.get("Extension") or "")
    except LaserficheError:
        # Detection falls back to content-type alone; extraction may still work.
        pass

    # Laserfiche entry names frequently omit the extension — it lives in the
    # entry's `extension` attribute instead. Fold it into the filename hint
    # so format detection works on names like "Contract 2024" + ext "pdf".
    if extension and not _Path(name).suffix:
        name = f"{name or entry_id}.{extension.lstrip('.')}"

    scratch = _Path(tempfile.mkdtemp(prefix="lf-edoc-"))
    # Sanitized: entry names may contain characters (":", "?", ...) that are
    # invalid in local file names on Windows.
    target = scratch / ops_extract.sanitize_filename(name, fallback=f"{entry_id}.bin")
    try:
        target.write_bytes(content)
        extracted = ops_extract.extract(target, content_type=content_type, filename=name or None)
    except ops_extract.ExtractionError as exc:
        hint = (
            "For scanned images there is no text layer to extract — use "
            "search_content, which reads Laserfiche's OCR index."
            if exc.slug in ("unsupported_format",)
            else "Use mode='bytes' for client-side handling if the raw file is needed."
        )
        return _edoc_error(
            entry_id,
            "text",
            exc.slug,
            content_type=content_type,
            byte_size=byte_size,
            message=exc.message,
            hint=hint,
        )
    except (OSError, ValueError) as exc:
        # A scratch-file failure (permissions, disk full, a path the OS
        # rejects) — return the contract shape, never a raw exception.
        return _edoc_error(
            entry_id,
            "text",
            "extraction_failed",
            content_type=content_type,
            byte_size=byte_size,
            message=f"Could not extract via a scratch file: {exc}",
            hint="Use mode='bytes' for client-side handling if the raw file is needed.",
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    out_of_range: list[int] = []
    if extracted.pages is not None:
        pages_total = len(extracted.pages)
        if page_spec is None:
            selected = list(range(pages_total))
        else:
            selected = [i for i in page_spec if i < pages_total]
            out_of_range = [i + 1 for i in page_spec if i >= pages_total]
            if not selected:
                return _edoc_error(
                    entry_id,
                    "text",
                    "pages_out_of_range",
                    content_type=content_type,
                    byte_size=byte_size,
                    message=(
                        f"None of the requested pages exist — this document has "
                        f"{pages_total} page(s), and {out_of_range} were requested."
                    ),
                )
        full = "\n".join(extracted.pages[i] for i in selected)
    else:
        if page_spec is not None:
            return _edoc_error(
                entry_id,
                "text",
                "pages_not_applicable",
                content_type=content_type,
                byte_size=byte_size,
                message=(
                    f"`pages` only applies to paginated documents; this entry "
                    f"extracts as one unit ({extracted.backend}). Use "
                    "`char_offset` to window into it instead."
                ),
            )
        full = extracted.text

    total_chars = len(full)
    windowed = full[char_offset:] if char_offset else full
    truncated = len(windowed) > text_char_limit
    if truncated:
        windowed = windowed[:text_char_limit]

    result: dict[str, Any] = {
        **base,
        "backend": extracted.backend,
        "text": windowed,
        "truncated": truncated,
        "char_offset": char_offset,
        "chars_available": total_chars,
        "next_char_offset": char_offset + len(windowed) if truncated else None,
    }
    if extracted.pages is not None:
        result["pages_total"] = len(extracted.pages)
        if page_spec is not None:
            result["pages_selected"] = [i + 1 for i in selected]
    if out_of_range:
        result["pages_out_of_range"] = out_of_range
    if extracted.warnings:
        result["warnings"] = extracted.warnings
    return result


@register(v2_name="laserfiche_document_get_edoc")
async def get_document_edoc(
    entry_id: Annotated[
        int,
        Field(description="Entry ID of an electronic document (not a folder).", ge=1),
    ],
    mode: Annotated[
        Literal["info", "bytes", "text"],
        Field(
            default="info",
            description=(
                "'info' (default): headers only, nothing downloaded. "
                "'text': extracted text (PDF, Office, mail, HTML, text/*) — "
                "prefer this. 'bytes': base64, capped; avoid for anything large."
            ),
        ),
    ] = "info",
    max_bytes: Annotated[
        int | None,
        Field(
            default=None,
            description="Per-call override of LF_EDOC_MAX_BYTES (25 MB) for mode='bytes'/'text'.",
            ge=1,
        ),
    ] = None,
    text_char_limit: Annotated[
        int,
        Field(
            default=50_000,
            description="Truncate extracted text after this many characters (mode='text' only).",
            ge=1,
        ),
    ] = 50_000,
    pages: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "1-based page selection for mode='text' on PDFs, e.g. '3', "
                "'4-9', '1,3,5-7'. Omit for all pages."
            ),
            examples=["4-9", "1,3,5-7"],
        ),
    ] = None,
    char_offset: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Skip this many chars of extracted text (mode='text'); pass "
                "back next_char_offset from the prior call to page through."
            ),
            ge=0,
        ),
    ] = 0,
) -> dict[str, Any]:
    """Inspect (info), read as text, or download (bytes) a document's edoc.

    ``mode="info"`` (default) reads only the response headers — size and
    content-type, no body transferred; safe on any size. ``byte_size`` is
    null when the server omits Content-Length.

    ``mode="text"`` — **prefer this for reading content.** Handles PDF,
    DOCX, PPTX, XLSX, EML, HTML, RTF and ``text/*``; format is detected
    from content-type and the entry's filename. OCR is not attempted — for
    scans, use ``search_content``, which reads Laserfiche's OCR index.
    Narrow long documents with ``pages`` and/or ``char_offset`` instead of
    reading them whole; ``truncated``/``next_char_offset`` drive paging.

    ``mode="bytes"`` — base64 payload. Avoid: it inflates the file ~4/3,
    tokenizes terribly, and many hosts cap a tool result at 1 MB, so the
    call often fails outright. Only for genuinely small files where the raw
    bytes are the deliverable.

    ``bytes``/``text`` are refused above ``LF_EDOC_MAX_BYTES`` (default
    25 MB); the ``size_exceeds_cap`` error carries ``byte_size`` and
    ``max_bytes`` so you can decide whether to raise the cap and retry.
    Other failure slugs: ``not_found`` (folder or no edoc), ``auth_failed``,
    ``pdf_encrypted``, ``unsupported_format`` (scans — use search_content),
    ``legacy_office_format``, ``pages_out_of_range``, ``invalid_page_spec``.
    Failures always come back as ``mode="error"`` with the requested mode
    preserved in ``requested_mode``.
    """
    settings = get_settings()
    effective_cap = max_bytes if max_bytes is not None else settings.edoc_max_bytes

    client = _app.get_client()

    # 'info' is a metadata probe — resolve it from response headers so callers
    # can size a document without paying to transfer it.
    if mode == "info":
        try:
            probe_size, probe_type = await client.export_entry_meta_only(entry_id, part="Edoc")
        except LaserficheError as exc:
            return classify_lf_error("get_document_edoc", exc, entry_id=entry_id)
        return _edoc_info_response(entry_id, probe_size, probe_type)

    if mode == "text":
        page_spec, page_error = parse_page_spec(pages)
        if page_error is not None:
            return _edoc_error(entry_id, "text", "invalid_page_spec", message=page_error)
    else:
        page_spec = None

    try:
        content, content_type = await client.export_entry_with_meta(entry_id, part="Edoc")
    except LaserficheError as exc:
        return classify_lf_error("get_document_edoc", exc, entry_id=entry_id)

    byte_size = len(content)

    if byte_size > effective_cap:
        return _edoc_size_cap_response(entry_id, mode, byte_size, effective_cap, content_type)

    if mode == "bytes":
        return _edoc_bytes_response(entry_id, content, byte_size, content_type)

    ct_lower = (content_type or "").lower().split(";")[0].strip()
    # text/html and text/rtf carry markup, not readable text — they route
    # through the ops extractors below so the model sees stripped text, not
    # raw tags with script/style bodies. Other text/* decode directly.
    if ct_lower == "application/pdf" or (
        ct_lower.startswith("text/") and ct_lower not in ("text/html", "text/rtf")
    ):
        return _edoc_text_response(
            entry_id,
            content,
            byte_size,
            content_type,
            text_char_limit,
            page_spec=page_spec,
            char_offset=char_offset,
        )

    # Everything else — office formats, mail, HTML, and the octet-stream
    # labels self-hosted servers put on most edocs — goes through the same
    # extractors the CLI uses.
    return await _edoc_extract_via_ops(
        client,
        entry_id,
        content,
        byte_size,
        content_type,
        text_char_limit,
        page_spec=page_spec,
        char_offset=char_offset,
    )
