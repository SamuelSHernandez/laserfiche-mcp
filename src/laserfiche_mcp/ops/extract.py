"""Text extraction from document bytes, by format.

Laserfiche repositories are mostly PDFs and Office documents. The MCP's
``mode="text"`` handles PDF and ``text/*`` and returns
``unsupported_content_type`` for everything else, which leaves a large share
of a real repository unreadable without downloading the raw file.

Everything here operates on a file already on disk, never on a bytes blob in
memory — a 400 MB scan should cost one file handle, not 400 MB of RSS.

Dependency policy: PDF (pypdf) is already a hard dependency. DOCX, PPTX,
EML, HTML and RTF are handled with nothing but the standard library — they
are all either zip-of-XML or plain parsing. XLSX needs ``openpyxl`` and true
Outlook ``.msg`` needs ``extract-msg``; both are optional extras, imported
lazily, and their absence produces an actionable error rather than a
traceback.

Legacy binary ``.doc`` / ``.xls`` are deliberately unsupported: correct
extraction needs a system binary, and shipping that quietly would break the
"pip install and it works" promise.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

# OOXML namespaces. Declared rather than wildcarded so a malformed document
# can't smuggle text in from an unexpected namespace.
_NS_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NS_DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

_EXTENSION_KINDS: dict[str, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "docm": "docx",
    "pptx": "pptx",
    "pptm": "pptx",
    "xlsx": "xlsx",
    "xlsm": "xlsx",
    "eml": "eml",
    "msg": "msg",
    "htm": "html",
    "html": "html",
    "rtf": "rtf",
    "txt": "text",
    "csv": "text",
    "tsv": "text",
    "json": "text",
    "xml": "text",
    "md": "text",
    "log": "text",
    "doc": "legacy_office",
    "xls": "legacy_office",
    "ppt": "legacy_office",
}

_CONTENT_TYPE_KINDS: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "message/rfc822": "eml",
    "application/vnd.ms-outlook": "msg",
    "text/html": "html",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "application/msword": "legacy_office",
    "application/vnd.ms-excel": "legacy_office",
    "application/vnd.ms-powerpoint": "legacy_office",
}

# Formats whose text is naturally divided into pages/slides, so a hit can be
# reported as "page 4" rather than only a character offset.
PAGINATED_KINDS = frozenset({"pdf", "pptx"})

# Characters no Windows filesystem accepts in a file name, plus control chars.
# Laserfiche entry names allow several of these (`:`, `"`, `?`, ...), so an
# entry name cannot be used as a local file name without cleaning it first.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows device names that are invalid as file stems regardless of extension.
_WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def sanitize_filename(name: str, *, fallback: str = "document") -> str:
    """Reduce an entry name to a file name every filesystem accepts.

    Invalid characters become ``_``; trailing dots/spaces (rejected by
    Windows) are stripped; reserved device stems (``CON``, ``NUL``, ...) get
    an underscore prefix. The extension survives, which matters because
    format detection leans on it. Returns ``fallback`` when nothing usable
    remains.
    """
    # Substituting first also neutralizes path separators, so no path
    # component of `name` can survive into the result.
    cleaned = _INVALID_FILENAME_CHARS.sub("_", name).strip().rstrip(". ")
    if not cleaned or set(cleaned) <= {"_", "."}:
        return fallback
    stem = cleaned.split(".", 1)[0].lower()
    if stem in _WINDOWS_RESERVED_STEMS:
        cleaned = "_" + cleaned
    return cleaned


class ExtractionError(Exception):
    """Extraction could not proceed. ``slug`` is stable enough to branch on."""

    def __init__(self, slug: str, message: str) -> None:
        super().__init__(message)
        self.slug = slug
        self.message = message


@dataclass
class ExtractedText:
    """Result of a successful extraction.

    ``pages`` is populated only for paginated formats; for everything else
    the whole document is one unit and ``pages`` is None. ``backend`` names
    what actually did the work, so a surprising result is traceable.
    """

    text: str
    backend: str
    pages: list[str] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int | None:
        return len(self.pages) if self.pages is not None else None


def detect_kind(content_type: str | None, filename: str | None) -> str:
    """Identify the format from content-type, falling back to the extension.

    Content-type is checked first but is frequently useless — self-hosted
    Laserfiche hands back ``application/octet-stream`` for most edocs — so
    the filename extension is the practical discriminator.

    Returns a kind slug, or ``"unknown"`` when neither signal resolves.
    """
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in _CONTENT_TYPE_KINDS:
            return _CONTENT_TYPE_KINDS[base]
        if base.startswith("text/"):
            return "text"

    if filename:
        suffix = Path(filename).suffix.lstrip(".").lower()
        if suffix in _EXTENSION_KINDS:
            return _EXTENSION_KINDS[suffix]

    return "unknown"


# --- per-format extractors ---------------------------------------------------


def _extract_pdf(path: Path) -> ExtractedText:
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover - pypdf is a hard dependency
        raise ExtractionError(
            "backend_unavailable",
            "pypdf is required to read PDFs. Install with `pip install pypdf`.",
        ) from exc

    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 — pypdf raises many subclasses
        raise ExtractionError(
            "pdf_open_failed", f"Could not open PDF ({type(exc).__name__}): {exc}"
        ) from exc

    if reader.is_encrypted:
        raise ExtractionError(
            "pdf_encrypted",
            "PDF is password-protected; text extraction is not possible.",
        )

    pages: list[str] = []
    warnings: list[str] = []
    for index, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 — partial extraction is useful
            pages.append("")
            warnings.append(f"page {index + 1}: extraction failed ({type(exc).__name__})")

    if not any(p.strip() for p in pages):
        warnings.append(
            "No text layer found — this is probably a scan that was never OCR'd, "
            "or whose OCR lives only in Laserfiche's index. Try the `search` "
            "command instead, which reads that index."
        )

    return ExtractedText(text="\n".join(pages), backend="pypdf", pages=pages, warnings=warnings)


def _open_ooxml(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ExtractionError(
            "not_a_zip",
            "File is not a valid OOXML package (expected a zip container). "
            "It may be a legacy binary Office file with a modern extension.",
        ) from exc


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    """Flatten one ``<w:p>`` into a line, honoring tabs and explicit breaks."""
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_NS_WORD}t":
            parts.append(node.text or "")
        elif node.tag == f"{_NS_WORD}tab":
            parts.append("\t")
        elif node.tag == f"{_NS_WORD}br":
            parts.append("\n")
    return "".join(parts)


def _extract_docx(path: Path) -> ExtractedText:
    with _open_ooxml(path) as archive:
        try:
            raw = archive.read("word/document.xml")
        except KeyError as exc:
            raise ExtractionError(
                "malformed_docx", "Zip container has no word/document.xml part."
            ) from exc

    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ExtractionError("malformed_docx", f"document.xml is not valid XML: {exc}") from exc

    lines = [_docx_paragraph_text(p) for p in root.iter(f"{_NS_WORD}p")]
    return ExtractedText(text="\n".join(lines).strip(), backend="docx")


def _extract_pptx(path: Path) -> ExtractedText:
    with _open_ooxml(path) as archive:
        # Sort numerically: slide10.xml must not sort between slide1 and slide2.
        names = [
            n for n in archive.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        ]
        names.sort(key=lambda n: int(re.sub(r"\D", "", Path(n).stem) or 0))

        slides: list[str] = []
        warnings: list[str] = []
        for name in names:
            try:
                root = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError:
                slides.append("")
                warnings.append(f"{name}: not valid XML, skipped")
                continue
            runs = [node.text or "" for node in root.iter(f"{_NS_DRAWING}t")]
            slides.append("\n".join(r for r in runs if r))

    return ExtractedText(
        text="\n\n".join(slides).strip(),
        backend="pptx",
        pages=slides,
        warnings=warnings,
    )


def _extract_xlsx(path: Path) -> ExtractedText:
    try:
        import openpyxl
    except ImportError as exc:
        raise ExtractionError(
            "backend_unavailable",
            "Reading .xlsx needs openpyxl. Install the extra with "
            "`pip install 'laserfiche-mcp[office]'`.",
        ) from exc

    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — openpyxl raises many subclasses
        raise ExtractionError(
            "xlsx_open_failed", f"Could not open workbook ({type(exc).__name__}): {exc}"
        ) from exc

    chunks: list[str] = []
    try:
        for sheet in book.worksheets:
            chunks.append(f"# {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                if row is None:
                    continue
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    chunks.append("\t".join(cells))
    finally:
        book.close()

    return ExtractedText(text="\n".join(chunks).strip(), backend="openpyxl")


def _extract_eml(path: Path) -> ExtractedText:
    from email import policy
    from email.parser import BytesParser

    with path.open("rb") as handle:
        message = BytesParser(policy=policy.default).parse(handle)

    header_names = ("From", "To", "Cc", "Date", "Subject")
    headers = [f"{name}: {message[name]}" for name in header_names if message[name]]

    body = ""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is not None:
        content = part.get_content()
        body = strip_html(content) if part.get_content_subtype() == "html" else content

    attachments = [a.get_filename() or "(unnamed)" for a in message.iter_attachments()]
    if attachments:
        headers.append(f"Attachments: {', '.join(attachments)}")

    return ExtractedText(text="\n".join(headers) + "\n\n" + body.strip(), backend="email")


def _extract_msg(path: Path) -> ExtractedText:
    try:
        import extract_msg
    except ImportError as exc:
        raise ExtractionError(
            "backend_unavailable",
            "Reading Outlook .msg needs extract-msg. Install the extra with "
            "`pip install 'laserfiche-mcp[office]'`. (Exporting the mail as "
            ".eml instead needs no extra dependency.)",
        ) from exc

    try:
        message = extract_msg.Message(str(path))
    except Exception as exc:  # noqa: BLE001 — extract_msg raises many subclasses
        raise ExtractionError(
            "msg_open_failed", f"Could not open .msg ({type(exc).__name__}): {exc}"
        ) from exc

    try:
        pairs = (
            ("From", message.sender),
            ("To", message.to),
            ("Cc", message.cc),
            ("Date", message.date),
            ("Subject", message.subject),
        )
        headers = [f"{label}: {value}" for label, value in pairs if value]
        body = message.body or ""
    finally:
        message.close()

    joined = "\n".join(headers)
    return ExtractedText(text=f"{joined}\n\n{str(body).strip()}", backend="extract-msg")


class _TextHarvester(HTMLParser):
    """Collect visible text, dropping the contents of script and style."""

    _SKIP = {"script", "style", "head"}
    _BREAK_AFTER = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._SKIP:
            self._suppress += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._suppress:
            self._suppress -= 1
        if tag in self._BREAK_AFTER:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self.parts.append(data)


def strip_html(markup: str) -> str:
    """Reduce HTML to its visible text. Best-effort, no dependencies."""
    harvester = _TextHarvester()
    harvester.feed(markup)
    harvester.close()
    text = "".join(harvester.parts)
    # Collapse the run of blank lines that block-level breaks leave behind.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_html(path: Path) -> ExtractedText:
    markup = path.read_text(encoding="utf-8", errors="replace")
    return ExtractedText(text=strip_html(markup), backend="html.parser")


# RTF groups whose contents are metadata, not document text.
_RTF_DISCARD_GROUPS = re.compile(
    r"\{\\\*?\\(?:fonttbl|colortbl|stylesheet|info|pict|object|header|footer)[^{}]*"
    r"(?:\{[^{}]*\}[^{}]*)*\}",
    re.IGNORECASE,
)
_RTF_CONTROL_WORD = re.compile(r"\\([a-zA-Z]+)(-?\d+)?[ ]?")
_RTF_HEX_ESCAPE = re.compile(r"\\'([0-9a-fA-F]{2})")


def _extract_rtf(path: Path) -> ExtractedText:
    """Strip RTF control words. Best-effort — good enough to search, not to render."""
    raw = path.read_text(encoding="latin-1", errors="replace")
    body = _RTF_DISCARD_GROUPS.sub("", raw)
    body = _RTF_HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]).decode("latin-1"), body)
    body = body.replace("\\par", "\n").replace("\\line", "\n").replace("\\tab", "\t")
    body = _RTF_CONTROL_WORD.sub("", body)
    body = body.replace("{", "").replace("}", "")
    return ExtractedText(
        text=re.sub(r"\n{3,}", "\n\n", body).strip(),
        backend="rtf(builtin)",
        warnings=["RTF stripping is best-effort; formatting-heavy files may read oddly."],
    )


def _extract_text(path: Path) -> ExtractedText:
    return ExtractedText(text=path.read_text(encoding="utf-8", errors="replace"), backend="decode")


_EXTRACTORS = {
    "pdf": _extract_pdf,
    "docx": _extract_docx,
    "pptx": _extract_pptx,
    "xlsx": _extract_xlsx,
    "eml": _extract_eml,
    "msg": _extract_msg,
    "html": _extract_html,
    "rtf": _extract_rtf,
    "text": _extract_text,
}

SUPPORTED_KINDS = frozenset(_EXTRACTORS)


def extract(
    path: Path,
    *,
    content_type: str | None = None,
    filename: str | None = None,
) -> ExtractedText:
    """Extract text from a document on disk.

    ``content_type`` and ``filename`` are both hints for format detection;
    pass whatever you have. ``filename`` is usually the more reliable of the
    two, since self-hosted Laserfiche labels most edocs
    ``application/octet-stream``.

    Raises ``ExtractionError`` with a stable ``slug`` when the format is
    unreadable — including ``unsupported_format`` for image-only documents
    and ``legacy_office_format`` for binary .doc/.xls/.ppt.
    """
    kind = detect_kind(content_type, filename or path.name)

    if kind == "legacy_office":
        raise ExtractionError(
            "legacy_office_format",
            "Legacy binary Office formats (.doc/.xls/.ppt) need an external "
            "converter such as LibreOffice. Re-save as .docx/.xlsx/.pptx, or "
            "use Laserfiche's own extracted text if the server indexed it.",
        )

    extractor = _EXTRACTORS.get(kind)
    if extractor is None:
        raise ExtractionError(
            "unsupported_format",
            f"No text extractor for {kind!r} (content-type={content_type!r}, "
            f"name={filename!r}). Supported: {', '.join(sorted(SUPPORTED_KINDS))}. "
            "Scanned images have no text layer — search Laserfiche's OCR index "
            "instead.",
        )

    return extractor(path)
