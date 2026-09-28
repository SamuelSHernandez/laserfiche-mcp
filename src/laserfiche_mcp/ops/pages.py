"""Page-selection parsing, shared by the MCP tools and the CLI.

Lives outside ``tools/`` so the CLI can import it without pulling in FastMCP
and constructing the MCP application as an import side effect.
"""

from __future__ import annotations

# Upper bound on a single "lo-hi" chunk's span. Without this, a spec like
# "1-999999999" (a typo, or an attempt to "delete every page") expands into
# a set with that many entries — multi-second, multi-GB — synchronously on
# the event loop (this runs inside `async def get_document_edoc` with no
# executor offload), blocking every other concurrent request for the
# duration. No real document has anywhere near this many pages.
_MAX_RANGE_SPAN = 100_000


def parse_page_spec(spec: str | None) -> tuple[list[int] | None, str | None]:
    """Parse a 1-based page selection like ``"3"``, ``"4-9"`` or ``"1,3,5-7"``.

    Returns ``(sorted_unique_zero_based_indices, None)`` on success, or
    ``(None, message)`` on a malformed spec. ``(None, None)`` means "no
    selection given — use every page".

    Rejecting a bad spec beats silently reading the whole document: a model
    that asked for pages 40-45 and got all 300 has no way to tell.
    """
    if spec is None or not spec.strip():
        return None, None

    pages: set[int] = set()
    for chunk in spec.split(","):
        part = chunk.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            lo_raw, _, hi_raw = part.partition("-")
            lo_s, hi_s = lo_raw.strip(), hi_raw.strip()
            if not lo_s.isdigit() or not hi_s.isdigit():
                return None, f"Malformed page range {part!r}. Use forms like '4-9'."
            lo, hi = int(lo_s), int(hi_s)
            if lo < 1 or hi < 1:
                return None, f"Page numbers are 1-based; got {part!r}."
            if lo > hi:
                return None, f"Page range {part!r} runs backwards ({lo} > {hi})."
            if hi - lo + 1 > _MAX_RANGE_SPAN:
                return None, (
                    f"Page range {part!r} spans {hi - lo + 1} pages, over the "
                    f"{_MAX_RANGE_SPAN}-page limit. No real document has this "
                    "many pages — check for a typo."
                )
            pages.update(range(lo - 1, hi))
        else:
            if not part.isdigit():
                return None, f"Malformed page number {part!r}. Use '3', '4-9' or '1,3,5-7'."
            if int(part) < 1:
                return None, f"Page numbers are 1-based; got {part!r}."
            pages.add(int(part) - 1)

    if not pages:
        return None, None
    return sorted(pages), None
