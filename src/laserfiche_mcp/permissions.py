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

import os
import re
import unicodedata

_PAGE_RANGE_RE = re.compile(r"^[1-9]\d*(-[1-9]\d*)?(,[1-9]\d*(-[1-9]\d*)?)*$")

_NAME_MAX_LENGTH = 128


def _normalize(p: str) -> str:
    # NFC-normalize before comparing: a deny/allow prefix and a path returned
    # by the server can spell the same non-ASCII segment with different
    # Unicode representations (e.g. NFC "é" vs NFD "e"+combining-accent).
    # Without this, those compare unequal and a fence meant to block the
    # path silently lets it through instead.
    return unicodedata.normalize("NFC", p).replace("/", "\\").lower().rstrip("\\")


def has_traversal_segment(path: str) -> bool:
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

    if has_traversal_segment(path):
        return False, (
            f"Path {path!r} contains a '..' traversal segment. Paths with "
            "'..' are rejected unconditionally; use the entry's fully "
            "qualified path instead."
        )

    deny = _parse_csv(deny_csv)
    for d in deny:
        if _matches_prefix(path, d):
            return False, (
                f"Path {path!r} is under denied prefix {d!r} (LF_WRITE_PATHS_DENY). Writes refused."
            )

    allow = _parse_csv(allow_csv)
    if allow and not any(_matches_prefix(path, a) for a in allow):
        return False, (
            f"Path {path!r} is outside the allowed write prefixes "
            f"(LF_WRITE_PATHS_ALLOW={allow}). Writes refused."
        )

    return True, None


# --- Local import source: always-on credential-file blocklist ----------------
#
# Defense in depth for ``import_document``, which reads a file off the MCP
# process's own disk and uploads it. Independent of LF_IMPORT_SOURCE_DIRS and
# applied even when that is "*": nobody legitimately files an SSH key, a cloud
# credentials file or a browser password store into a document repository, and
# if a model is steered into doing it the blast radius is the whole machine.
# This is a short list of well-known secret locations, NOT a security boundary
# (a fence directory is the boundary); it exists so the unfenced default is not
# also the default that can exfiltrate keys.

IMPORT_SOURCE_ANY = "*"  # LF_IMPORT_SOURCE_DIRS value meaning "any path, I accept that"

_SENSITIVE_DIR_NAMES = frozenset({".ssh", ".aws", ".gnupg", ".azure", ".kube"})
_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".netrc",
        "_netrc",
        ".git-credentials",
        ".pgpass",
        ".npmrc",
        ".pypirc",
        ".claude.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "login data",  # Chromium-family saved passwords
        "ntuser.dat",
        "shadow",
        "sudoers",
    }
)
_ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})
_SENSITIVE_SUFFIXES = (".key", ".pfx", ".p12", ".ppk", ".kdbx", ".keystore", ".jks")
# Directory trees (compared after realpath + normcase, as path segments).
_SENSITIVE_TREES = (
    ("proc",),
    ("sys",),
    ("dev",),
    ("windows", "system32", "config"),
    ("appdata", "roaming", "microsoft", "credentials"),
    ("appdata", "roaming", "microsoft", "protect"),
    ("appdata", "local", "microsoft", "credentials"),
)


def sensitive_source_reason(file_path: str) -> str | None:
    """Return why ``file_path`` is on the credential blocklist, or None if it isn't.

    Operates on the symlink-resolved path, so a harmless-looking link to
    ``~/.ssh/id_rsa`` is caught as well.
    """
    resolved = os.path.normcase(os.path.realpath(file_path))
    parts = [p.lower() for p in re.split(r"[\\/]+", resolved) if p]
    name = parts[-1] if parts else ""

    is_env_file = name == ".env" or (name.startswith(".env.") and name not in _ENV_TEMPLATE_NAMES)
    if name in _SENSITIVE_FILE_NAMES or is_env_file:
        return f"{name!r} is a credential/secret file"
    if name.endswith(_SENSITIVE_SUFFIXES):
        return f"{name!r} looks like private key material"
    for seg in parts[:-1]:
        if seg in _SENSITIVE_DIR_NAMES:
            return f"it is inside a {seg!r} credentials directory"
    lowered = list(parts)
    if lowered and lowered[0].endswith(":"):  # drop a Windows drive letter
        lowered = lowered[1:]
    for tree in _SENSITIVE_TREES:
        n = len(tree)
        # Windows trees may sit under any drive/profile, so match anywhere; the
        # POSIX roots (proc/sys/dev) only count when they are the first segment.
        starts = (0,) if tree[0] in ("proc", "sys", "dev") else range(len(lowered) - n + 1)
        if any(tuple(lowered[i : i + n]) == tree for i in starts if i >= 0):
            return f"it is under a protected system location ({'/'.join(tree)})"
    return None


def local_source_path_allowed(
    file_path: str,
    allow_csv: str | None,
) -> tuple[bool, str | None]:
    """Check a local filesystem source path against an allowed-directory list.

    Mirrors ``path_allowed`` but fences the *source* side of
    ``import_document`` (the local file the MCP process reads) rather
    than the repository destination. Returns ``(ok, reason)``.

    Behavior:
        * ``allow_csv`` unset/empty, or containing ``*`` -> always OK. Fencing
          is opt-in so existing single-user/single-machine deployments keep
          working unchanged until they set ``LF_IMPORT_SOURCE_DIRS``; ``*``
          is the explicit "I know, allow any path" form (it silences the
          startup warning). The credential-file blocklist
          (:func:`sensitive_source_reason`) applies either way.
        * Otherwise ``file_path`` must resolve (symlinks included, via
          ``os.path.realpath``) to a path equal to, or nested under, one
          of the configured directories. Resolving symlinks stops a
          symlink planted inside an allowed directory from pointing
          a read outside of it.
        * Comparison uses ``os.path.normcase`` so it's case-insensitive
          on case-insensitive filesystems (Windows) and case-sensitive
          elsewhere, matching platform path semantics.
    """
    allow = _parse_csv(allow_csv)
    if not allow or IMPORT_SOURCE_ANY in allow:
        return True, None

    resolved = os.path.normcase(os.path.realpath(file_path))
    for raw_dir in allow:
        # rstrip the separator first: os.path.realpath on a filesystem/drive
        # root (e.g. "C:/" on Windows) already returns a trailing separator,
        # and appending another before the startswith check would reject
        # every legitimate path under that root.
        allowed_dir = os.path.normcase(os.path.realpath(raw_dir)).rstrip(os.sep)
        if resolved == allowed_dir or resolved.startswith(allowed_dir + os.sep):
            return True, None

    return False, (
        f"File path {file_path!r} resolves outside the allowed import "
        f"source directories (LF_IMPORT_SOURCE_DIRS={allow})."
    )


def tool_allowed(
    tool_name: str | tuple[str, ...],
    allowed_csv: str | None,
) -> tuple[bool, str | None]:
    """Check ``tool_name`` against a comma-separated allowlist.

    ``tool_name`` may be a single name or a tuple of equivalent names for
    the same tool (e.g. ``(legacy_name, v2_name)``) — the tool passes if
    ANY of them appears in the allowlist, so an operator who configures
    ``LF_WRITE_TOOLS_ALLOWED`` with the README-recommended v2 names
    (``laserfiche_template_assign``) gets the same result as one who uses
    the legacy names (``assign_template``); a deployment mixing both
    naming schemes across entries also works.

    Returns ``(ok, reason)``. When ``allowed_csv`` is None or empty, all
    tools pass (no allowlist configured).
    """
    allowed = _parse_csv(allowed_csv)
    if not allowed:
        return True, None
    names = (tool_name,) if isinstance(tool_name, str) else tool_name
    if not any(n in allowed for n in names):
        shown = repr(names[0]) if len(names) == 1 else " / ".join(repr(n) for n in names)
        return False, (
            f"Tool {shown} is not in the configured allowlist (LF_WRITE_TOOLS_ALLOWED={allowed})."
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
                    f"page_range part {part!r} has start > end. Ranges must be ascending."
                )

    return True, None


def parse_tool_allowlist(allowed_csv: str | None) -> set[str] | None:
    """Return the parsed allowlist as a set, or None when unconfigured.

    Used by the server to filter write-tool registration at startup.
    """
    parsed = _parse_csv(allowed_csv)
    return set(parsed) if parsed else None
