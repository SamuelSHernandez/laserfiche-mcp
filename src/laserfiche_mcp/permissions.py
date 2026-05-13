"""Configuration-driven write guards: path scope fences and tool allowlists.

These are pure functions kept separate from the server module so they can
be unit-tested without spinning up a FastMCP instance.

Path matching rules:
    * Case-insensitive (Laserfiche paths are not case-sensitive).
    * Backslash-normalized — forward slashes are accepted in config and
      converted to backslashes before comparison.
    * Prefix-based — ``"\\\\Imports"`` matches ``"\\\\Imports\\\\2024\\\\foo"``
      but not ``"\\\\ImportsArchive"`` (the next character after the prefix
      must be a backslash or end-of-string).
    * Deny wins over allow. If a path matches any deny prefix, it's
      refused regardless of allow matches.
"""

from __future__ import annotations


def _normalize(p: str) -> str:
    return p.replace("/", "\\").lower().rstrip("\\")


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


def parse_tool_allowlist(allowed_csv: str | None) -> set[str] | None:
    """Return the parsed allowlist as a set, or None when unconfigured.

    Used by the server to filter write-tool registration at startup.
    """
    parsed = _parse_csv(allowed_csv)
    return set(parsed) if parsed else None
