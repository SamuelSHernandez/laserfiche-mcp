"""Configuration-driven write guards: path scope fences, name validation,
and tool allowlists.

These are pure functions kept separate from the server module so they can
be unit-tested without spinning up a FastMCP instance.

Path matching rules:
    * Case-insensitive (Laserfiche paths are not case-sensitive).
    * Backslash-normalized — forward slashes are accepted in config and
      converted to backslashes before comparison.
    * Prefix-based — ``"\\\\Imports"`` matches ``"\\\\Imports\\\\2024\\\\foo"``
      but not ``"\\\\ImportsArchive"`` (the next character after the prefix
      must be a backslash or end-of-string).
    * ``..`` segments are rejected unconditionally (defense-in-depth
      against path-traversal). Server-side ACL is the real fence, but
      we don't pretend a path containing ``..`` is meaningful.
    * Deny wins over allow. If a path matches any deny prefix, it's
      refused regardless of allow matches.

Name validation rules (entry names — folders, documents, etc.):
    * No backslashes or forward slashes (would be misread as path segments).
    * No NULL bytes or control characters (Laserfiche stores names as
      UTF-16 strings; control chars are rejected server-side anyway).
    * Length 1–128 after stripping leading/trailing whitespace.

Page range syntax (for delete_pages):
    * Comma-separated list of single page numbers or hyphenated ranges:
      ``"1"``, ``"1,2,3"``, ``"1-3"``, ``"1-3,5,7-9"``.
    * No spaces, no negative numbers, no zero.
"""

from __future__ import annotations

import re

_PAGE_RANGE_RE = re.compile(r"^[1-9]\d*(-[1-9]\d*)?(,[1-9]\d*(-[1-9]\d*)?)*$")

_NAME_MAX_LENGTH = 128


def _normalize(p: str) -> str:
    return p.replace("/", "\\").lower().rstrip("\\")


def _has_traversal_segment(path: str) -> bool:
    """True if any segment of ``path`` is exactly ``..`` after normalization."""
    normalized = path.replace("/", "\\")
    return any(seg == ".." for seg in normalized.split("\\"))


def _parse_csv(raw: str | None) -> list[str]:
    """Parse a comma-separated env-var string into a list of stripped entries.

    Empty entries (from extra commas or whitespace) are dropped. Returns
    an empty list when ``raw`` is None or blank.
    """
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _matches_prefix(path: str, prefix: str) -> bool:
    """Prefix match with a boundary requirement.

    ``\\\\Imports`` matches ``\\\\Imports`` and ``\\\\Imports\\\\anything``,
    but NOT ``\\\\ImportsArchive``.
    """
    np = _normalize(path)
    nx = _normalize(prefix)
    if np == nx:
        return True
    return np.startswith(nx + "\\")


def path_allowed(
    path: str | None,
    allow_csv: str | None,
    deny_csv: str | None,
) -> tuple[bool, str | None]:
    """Check ``path`` against allow/deny prefix lists.

    Returns ``(ok, reason)``. ``reason`` is None on success.

    Behavior:
        * No allow_csv and no deny_csv → always OK.
        * deny_csv only → OK unless ``path`` matches a deny prefix.
        * allow_csv only → OK only if ``path`` matches an allow prefix.
        * Both → must match allow AND not match deny.
        * ``path`` is None → OK (we can't enforce a path fence when we
          don't know where the operation lands).
    """
    if path is None:
        return True, None

    if _has_traversal_segment(path):
        return False, (
            f"Path {path!r} contains a '..' traversal segment. Paths with "
            "'..' are rejected unconditionally; use the entry's fully "
            "qualified path instead."
        )

    deny = _parse_csv(deny_csv)
    for d in deny:
        if _matches_prefix(path, d):
            return False, (
                f"Path {path!r} is under denied prefix {d!r} "
                "(LF_WRITE_PATHS_DENY). Writes refused."
            )

    allow = _parse_csv(allow_csv)
    if allow and not any(_matches_prefix(path, a) for a in allow):
        return False, (
            f"Path {path!r} is outside the allowed write prefixes "
            f"(LF_WRITE_PATHS_ALLOW={allow}). Writes refused."
        )

    return True, None


def tool_allowed(
    tool_name: str,
    allowed_csv: str | None,
) -> tuple[bool, str | None]:
    """Check ``tool_name`` against a comma-separated allowlist.

    Returns ``(ok, reason)``. When ``allowed_csv`` is None or empty, all
    tools pass (no allowlist configured).
    """
    allowed = _parse_csv(allowed_csv)
    if not allowed:
        return True, None
    if tool_name not in allowed:
        return False, (
            f"Tool {tool_name!r} is not in the configured allowlist "
            f"(LF_WRITE_TOOLS_ALLOWED={allowed})."
        )
    return True, None


def name_allowed(name: str) -> tuple[bool, str | None]:
    """Validate an entry name for use in create/rename/move/import operations.

    Returns ``(ok, reason)``. ``reason`` is None on success.

    Rules:
        * Stripped length must be 1–128.
        * No backslash, forward slash, or NULL byte.
        * No ASCII control characters (anything below U+0020).
    """
    if not isinstance(name, str):
        return False, "Entry name must be a string."

    stripped = name.strip()
    if not stripped:
        return False, "Entry name cannot be empty or whitespace-only."
    if len(stripped) > _NAME_MAX_LENGTH:
        return False, (
            f"Entry name length {len(stripped)} exceeds the maximum "
            f"of {_NAME_MAX_LENGTH} characters."
        )

    for forbidden in ("\\", "/", "\x00"):
        if forbidden in stripped:
            display = repr(forbidden)
            return False, (
                f"Entry name contains {display}, which is not allowed. "
                "Names cannot contain backslashes, forward slashes, or "
                "NULL bytes."
            )

    for ch in stripped:
        if ord(ch) < 0x20:
            return False, (
                f"Entry name contains a control character (U+{ord(ch):04X}). "
                "Control characters are rejected."
            )

    return True, None


def validate_page_range(range_str: str) -> tuple[bool, str | None]:
    """Validate a page-range expression for delete_pages.

    Returns ``(ok, reason)``. ``reason`` is None on success.

    Accepted syntax: ``"1"``, ``"1,2,3"``, ``"1-3"``, ``"1-3,5,7-9"``.
    No spaces, no leading zeros, no negative numbers, no zero, no
    trailing comma. Empty input is also rejected (caller usually
    catches this earlier with a dedicated error slug).
    """
    if not isinstance(range_str, str):
        return False, "page_range must be a string."

    if not range_str or not range_str.strip():
        return False, "page_range cannot be empty or whitespace-only."

    stripped = range_str.strip()
    if " " in stripped:
        return False, "page_range cannot contain spaces."

    if not _PAGE_RANGE_RE.match(stripped):
        return False, (
            f"page_range {range_str!r} is not valid. Use single pages "
            "or hyphenated ranges separated by commas, e.g. "
            "'1', '1,2,3', '1-3', '1-3,5,7-9'. No spaces, leading "
            "zeros, or zero/negative numbers."
        )

    # Reject ranges where start > end (regex allows the syntax but it's nonsensical).
    for part in stripped.split(","):
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            if int(start_str) > int(end_str):
                return False, (
                    f"page_range part {part!r} has start > end. Ranges "
                    "must be ascending."
                )

    return True, None


def parse_tool_allowlist(allowed_csv: str | None) -> set[str] | None:
    """Return the parsed allowlist as a set, or None when unconfigured.

    Used by the server to filter write-tool registration at startup.
    """
    parsed = _parse_csv(allowed_csv)
    return set(parsed) if parsed else None
