"""Property-based tests for the two parser-like layers: search.py's query
repair and permissions.py's path matcher — a security-relevant fence.

Tracked as an open hardening item in docs/internal/TODO.md ("How to find
more bugs going forward" — property-based tests for repair_escape_quotes,
repair_wildcard_name, and _matches_prefix). The example-based tests
elsewhere cover the cases someone thought to write by hand; these sweep
for the ones nobody did.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from laserfiche_mcp.permissions import _matches_prefix, path_allowed
from laserfiche_mcp.search import repair_escape_quotes, repair_wildcard_name

# ASCII-only: ``str.lower()``/``str.upper()`` aren't perfectly symmetric
# for every Unicode code point (German ß, Turkish dotless i, ...) — that's
# a Python casing quirk, not something these fences claim to handle, and
# using it would make the case-insensitivity property flaky for reasons
# unrelated to the code under test. Laserfiche paths in practice are
# overwhelmingly ASCII anyway.
_ASCII_TEXT = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=100)
_ASCII_WORD = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12)

# --- search.py: repair_escape_quotes ----------------------------------------


@given(_ASCII_TEXT)
@settings(max_examples=300)
def test_repair_escape_quotes_never_raises(query: str) -> None:
    repair_escape_quotes(query)


@given(_ASCII_TEXT)
@settings(max_examples=300)
def test_repair_escape_quotes_is_idempotent(query: str) -> None:
    """Running repair on its own output must find nothing left to repair —
    otherwise a single search_natural retry could still leave a query the
    server rejects, corrupting it further instead of converging."""
    repaired = repair_escape_quotes(query)
    if repaired is None:
        return
    assert repair_escape_quotes(repaired) is None


@given(_ASCII_TEXT)
@settings(max_examples=300)
def test_repair_wildcard_name_never_raises(query: str) -> None:
    repair_wildcard_name(query)


@given(_ASCII_TEXT)
@settings(max_examples=300)
def test_repair_wildcard_name_is_idempotent(query: str) -> None:
    rewritten = repair_wildcard_name(query)
    if rewritten is None:
        return
    assert repair_wildcard_name(rewritten) is None


# --- permissions.py: the path fence -----------------------------------------


@given(
    path=_ASCII_TEXT,
    allow_csv=st.one_of(st.none(), _ASCII_TEXT),
    deny_csv=st.one_of(st.none(), _ASCII_TEXT),
)
@settings(max_examples=300)
def test_path_allowed_never_raises(path: str, allow_csv: str | None, deny_csv: str | None) -> None:
    path_allowed(path, allow_csv, deny_csv)


@given(
    segments=st.lists(_ASCII_WORD, max_size=4),
    allow_csv=st.one_of(st.none(), _ASCII_TEXT),
    deny_csv=st.one_of(st.none(), _ASCII_TEXT),
)
@settings(max_examples=300)
def test_traversal_segment_always_blocked_regardless_of_config(
    segments: list[str], allow_csv: str | None, deny_csv: str | None
) -> None:
    """A '..' segment is rejected unconditionally — the module's own
    documented guarantee — no matter what allow_csv/deny_csv say,
    including when they'd otherwise permit or deny this exact path."""
    path = "\\" + "\\".join([*segments, "..", *segments])
    ok, reason = path_allowed(path, allow_csv, deny_csv)
    assert ok is False
    assert reason is not None and "traversal" in reason.lower()


@given(prefix=_ASCII_WORD, suffix=st.one_of(st.none(), _ASCII_WORD))
@settings(max_examples=300)
def test_deny_wins_when_a_path_matches_both_allow_and_deny(prefix: str, suffix: str | None) -> None:
    path = f"\\{prefix}\\{suffix}" if suffix else f"\\{prefix}"
    ok, reason = path_allowed(path, allow_csv=f"\\{prefix}", deny_csv=f"\\{prefix}")
    assert ok is False
    assert reason is not None


@given(path=_ASCII_WORD, prefix=_ASCII_WORD)
@settings(max_examples=300)
def test_matches_prefix_is_case_insensitive(path: str, prefix: str) -> None:
    """Laserfiche paths are not case-sensitive — the module's own stated
    rule — so a match result must not depend on which side happens to be
    upper/lower case."""
    result = _matches_prefix(path, prefix)
    assert _matches_prefix(path.upper(), prefix.lower()) == result
    assert _matches_prefix(path.lower(), prefix.upper()) == result
