"""Search inside one document's text — grep, with page numbers.

The token argument for this module: reading a 200-page contract to answer
"does it mention indemnification" moves ~400,000 characters to answer a
yes/no. Matching locally and returning three 200-character windows answers
the same question for a rounding error, and does it deterministically.

Deliberately mirrors the shape of ``search_content``'s context hits, so a
repository-wide search and a single-document search read the same way.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from .extract import ExtractedText


@dataclass(frozen=True)
class Match:
    """One match, with enough context to judge relevance without opening the file."""

    page: int | None
    """1-based page for paginated formats; None when the format has no pages."""
    line: int
    """1-based line number within the document (or within the page, if paginated)."""
    offset: int
    """Character offset of the match within the searched unit."""
    matched: str
    """The exact text that matched."""
    context: str
    """Surrounding text, trimmed to the requested window."""


def compile_pattern(pattern: str, *, regex: bool, ignore_case: bool) -> re.Pattern[str]:
    """Build the search pattern.

    Literal is the default because most searches are for a phrase, and a
    phrase containing ``(`` or ``.`` should not silently become a regex.

    Raises ``re.error`` on an invalid pattern; callers should surface that
    verbatim rather than guessing at a repair — a bad regex is a typo the
    user needs to see.
    """
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(pattern if regex else re.escape(pattern), flags)


def _window(text: str, start: int, end: int, context_chars: int) -> str:
    """Slice a context window centered on [start, end), marking any elision."""
    if context_chars <= 0:
        return text[start:end]

    span = end - start
    slack = max(0, context_chars - span)
    left = max(0, start - slack // 2)
    right = min(len(text), left + context_chars)
    left = max(0, right - context_chars)

    excerpt = text[left:right].replace("\n", " ").replace("\r", " ").strip()
    excerpt = re.sub(r"\s{2,}", " ", excerpt)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{excerpt}{suffix}"


def _search_unit(
    text: str,
    compiled: re.Pattern[str],
    *,
    page: int | None,
    context_chars: int,
) -> Iterator[Match]:
    """Yield every match inside one unit of text (a page, or a whole document)."""
    # Precompute line starts once so line numbers cost a bisect, not a count
    # per match — a document with thousands of hits shouldn't be quadratic.
    line_starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            line_starts.append(index + 1)

    import bisect

    for found in compiled.finditer(text):
        start, end = found.span()
        line_index = bisect.bisect_right(line_starts, start) - 1
        yield Match(
            page=page,
            line=line_index + 1,
            offset=start,
            matched=found.group(0),
            context=_window(text, start, end, context_chars),
        )


def find_in_text(
    extracted: ExtractedText,
    pattern: str,
    *,
    regex: bool = False,
    ignore_case: bool = True,
    context_chars: int = 200,
    max_matches: int = 50,
) -> tuple[list[Match], int]:
    """Find ``pattern`` in an extracted document.

    Returns ``(matches, total_found)``. ``total_found`` counts every match
    even when ``matches`` is capped at ``max_matches``, so a caller can tell
    "3 hits" from "the first 3 of 900" — the distinction that decides whether
    a search was specific enough.

    Paginated formats are searched page by page so each match carries a real
    page number; everything else is searched as one unit.
    """
    compiled = compile_pattern(pattern, regex=regex, ignore_case=ignore_case)

    matches: list[Match] = []
    total = 0

    if extracted.pages is not None:
        units: list[tuple[int | None, str]] = [
            (number, body) for number, body in enumerate(extracted.pages, start=1)
        ]
    else:
        units = [(None, extracted.text)]

    for page, body in units:
        for match in _search_unit(body, compiled, page=page, context_chars=context_chars):
            total += 1
            if len(matches) < max_matches:
                matches.append(match)

    return matches, total
