"""Tools for reading the bytes / extracted text of an electronic document."""

from __future__ import annotations

import base64
import io
from typing import Any, Literal

from .. import _app
from .._app import get_settings
from ..errors import LaserficheError, classify_lf_error
from ._registry import register


@register(v2_name="laserfiche_document_get_text")
async def get_document_text(entry_id: int, max_chars: int = 50_000) -> dict[str, Any]:
    """Download a document's server-extracted text (v2-only).

    Use for "summarize this document", "what does this say", or any other
    task that needs the readable contents of a document rather than the
    raw binary. The text comes from Laserfiche's own extraction pipeline
    (OCR for image documents, upstream extraction for office files), so
    you get clean text without having to parse a PDF yourself.

    **v1 servers do not expose this endpoint.** If your deployment is on
    v1 (the default), this tool returns a structured error at the client
    layer. Use ``get_document_edoc(entry_id, mode="text")`` instead — it
    fetches the raw edoc and extracts text client-side (pypdf for PDFs,
    direct decode for ``text/*`` MIME types).

    Args:
        entry_id: Integer entry ID of an electronic document (not a folder).
        max_chars: Truncate the returned text after this many characters
            (default 50,000). The response's ``truncated`` field signals
            whether truncation occurred.

    Returns: ``{"entry_id": <int>, "text": <str>, "char_count": <int>,
    "truncated": <bool>}`` on success.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found`` (entry is a
    folder, or has no extracted text), ``method_not_allowed`` /
    ``server_error`` (v1 server — fall back to ``get_document_edoc``).
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


def _extract_pdf_text(content: bytes, char_limit: int) -> dict[str, Any]:
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
    chunks: list[str] = []
    pages_extracted = 0
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
            pages_extracted += 1
        except Exception as exc:  # noqa: BLE001 — partial extraction is acceptable
            chunks.append(f"[page extraction failed: {type(exc).__name__}]")

    full = "\n".join(chunks)
    truncated = len(full) > char_limit
    if truncated:
        full = full[:char_limit] + f"\n\n[truncated, {len(chunks)} pages total]"

    return {
        "ok": True,
        "text": full,
        "pages_total": pages_total,
        "pages_extracted": pages_extracted,
        "truncated": truncated,
    }


def _edoc_info_response(
    entry_id: int,
    byte_size: int,
    content_type: str | None,
) -> dict[str, Any]:
    """``mode='info'`` payload — metadata only, no bytes returned to the model."""
    return {
        "entry_id": entry_id,
        "mode": "info",
        "byte_size": byte_size,
        "content_type": content_type,
        "hint": (
            "Raw bytes were fetched but not returned to the model. "
            "Use mode='bytes' for the base64 payload or mode='text' "
            "for server-side extracted text."
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
    return {
        "entry_id": entry_id,
        "mode": mode,
        "error": "size_exceeds_cap",
        "byte_size": byte_size,
        "max_bytes": effective_cap,
        "content_type": content_type,
        "message": (
            f"Edoc is {byte_size} bytes, which exceeds the {effective_cap}-byte cap. "
            "Pass max_bytes=<larger value> or raise LF_EDOC_MAX_BYTES "
            "if you really need this document."
        ),
    }


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
) -> dict[str, Any]:
    """Extract text from the edoc based on content-type."""
    ct_lower = (content_type or "").lower().split(";")[0].strip()

    if ct_lower == "application/pdf":
        result = _extract_pdf_text(content, text_char_limit)
        base = {
            "entry_id": entry_id,
            "mode": "text",
            "content_type": content_type,
            "byte_size": byte_size,
        }
        if result.get("ok"):
            return {
                **base,
                "text": result["text"],
                "pages_total": result["pages_total"],
                "pages_extracted": result["pages_extracted"],
                "truncated": result["truncated"],
            }
        return {
            **base,
            "error": result.get("error", "pdf_extraction_failed"),
            "message": result.get("message"),
            "exception_class": result.get("exception_class"),
            "hint": "Try mode='bytes' to retrieve the raw PDF for client-side handling.",
        }

    if ct_lower.startswith("text/"):
        text = content.decode("utf-8", errors="replace")
        truncated = len(text) > text_char_limit
        if truncated:
            text = text[:text_char_limit] + "\n\n[truncated]"
        return {
            "entry_id": entry_id,
            "mode": "text",
            "content_type": content_type,
            "byte_size": byte_size,
            "text": text,
            "truncated": truncated,
        }

    return {
        "entry_id": entry_id,
        "mode": "text",
        "content_type": content_type,
        "byte_size": byte_size,
        "error": "unsupported_content_type",
        "message": (
            f"Cannot extract text from content-type {content_type!r}. "
            "Server-side text extraction is implemented only for "
            "application/pdf and text/*. Use mode='bytes' to download "
            "the file and handle it client-side."
        ),
    }


@register(v2_name="laserfiche_document_get_edoc")
async def get_document_edoc(
    entry_id: int,
    mode: Literal["info", "bytes", "text"] = "info",
    max_bytes: int | None = None,
    text_char_limit: int = 50_000,
) -> dict[str, Any]:
    """Download or inspect a document's raw electronic file (edoc).

    The recommended path for reading document content on v1 servers
    (``get_document_text`` has no endpoint to call there). Three modes
    trade off cost vs. depth:

    Args:
        entry_id: Integer entry ID. Must point to an electronic document,
            not a folder.
        mode:
            ``"info"`` *(default)* — fetches the edoc but returns only its
            size and content-type, plus a hint. No bytes enter the model's
            context. Cheapest; safe to call on anything as a first probe.

            ``"bytes"`` — returns the edoc as base64-encoded bytes plus
            content-type and size. Refused if the edoc exceeds
            ``LF_EDOC_MAX_BYTES`` (default 25 MB) — see ``max_bytes``.

            ``"text"`` — extracts readable text server-side:

            - ``application/pdf`` → pypdf, page by page, truncated to
              ``text_char_limit``. Response includes ``pages_total``,
              ``pages_extracted``, ``truncated``.
            - ``text/*`` → decoded directly as UTF-8 (replacement chars
              on bad bytes).
            - Anything else (.docx, .xlsx, images, etc.) → structured
              error naming the content-type and suggesting ``mode="bytes"``
              for client-side handling. OCR is not attempted.
            - Encrypted or malformed PDFs → structured error with the
              underlying exception class.
        max_bytes: Per-call override for ``LF_EDOC_MAX_BYTES``. Use to
            raise the cap for a specific large document without changing
            the server-wide default.
        text_char_limit: Truncate extracted text after this many
            characters (default 50,000). Truncation is signalled by the
            ``truncated`` field, NOT a marker in the text itself.

    Returns: Always a dict. Shape depends on ``mode`` — see above.
    On size-cap refusal, response contains ``error="size_exceeds_cap"``
    plus ``byte_size`` and ``max_bytes`` so the LLM can decide whether
    to raise the cap and retry.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "entry_id": <int>, ...}``. Common slugs: ``not_found`` (entry is a
    folder, or has no edoc), ``auth_failed``.
    """
    settings = get_settings()
    effective_cap = max_bytes if max_bytes is not None else settings.edoc_max_bytes

    try:
        content, content_type = await _app.get_client().export_entry_with_meta(
            entry_id,
            part="Edoc",
        )
    except LaserficheError as exc:
        return classify_lf_error("get_document_edoc", exc, entry_id=entry_id)

    byte_size = len(content)

    if mode == "info":
        return _edoc_info_response(entry_id, byte_size, content_type)

    if byte_size > effective_cap:
        return _edoc_size_cap_response(entry_id, mode, byte_size, effective_cap, content_type)

    if mode == "bytes":
        return _edoc_bytes_response(entry_id, content, byte_size, content_type)

    return _edoc_text_response(entry_id, content, byte_size, content_type, text_char_limit)
