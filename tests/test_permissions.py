"""Tests for the permissions module (path scope + tool allowlist)."""

from __future__ import annotations

from laserfiche_mcp import permissions

# --- path_allowed -----------------------------------------------------------


def test_no_lists_means_allowed() -> None:
    ok, reason = permissions.path_allowed("\\Anywhere\\foo", None, None)
    assert ok is True
    assert reason is None


def test_none_path_is_allowed() -> None:
    """When the caller can't supply a path, we don't enforce."""
    ok, _ = permissions.path_allowed(None, "\\Allowed", "\\Denied")
    assert ok is True


def test_deny_matches_path_under_prefix() -> None:
    ok, reason = permissions.path_allowed(
        "\\Trash\\foo", None, "\\Trash",
    )
    assert ok is False
    assert reason is not None
    assert "Trash" in reason


def test_deny_does_not_match_sibling() -> None:
    """`\\TrashArchive` is not `\\Trash` — prefix needs boundary."""
    ok, _ = permissions.path_allowed(
        "\\TrashArchive\\foo", None, "\\Trash",
    )
    assert ok is True


def test_allow_matches_path_under_prefix() -> None:
    ok, _ = permissions.path_allowed(
        "\\Imports\\2024\\foo", "\\Imports", None,
    )
    assert ok is True


def test_allow_rejects_path_outside_prefix() -> None:
    ok, reason = permissions.path_allowed(
        "\\Other\\foo", "\\Imports", None,
    )
    assert ok is False
    assert reason is not None
    assert "outside" in reason.lower() or "allowed" in reason.lower()


def test_allow_csv_supports_multiple_prefixes() -> None:
    ok, _ = permissions.path_allowed(
        "\\Archive\\WIP\\x", "\\Imports,\\Archive\\WIP", None,
    )
    assert ok is True


def test_deny_wins_over_allow() -> None:
    """A path can be in allow AND deny — deny wins."""
    ok, _ = permissions.path_allowed(
        "\\Imports\\Trash\\foo",
        "\\Imports",
        "\\Imports\\Trash",
    )
    assert ok is False


def test_path_matching_is_case_insensitive() -> None:
    ok, _ = permissions.path_allowed(
        "\\IMPORTS\\foo", "\\imports", None,
    )
    assert ok is True


def test_forward_slashes_in_config_are_normalized() -> None:
    """Operator-friendly: accept forward slashes in env var even though
    Laserfiche paths use backslashes."""
    ok, _ = permissions.path_allowed(
        "\\Imports\\foo", "/Imports", None,
    )
    assert ok is True


def test_extra_commas_and_whitespace_are_tolerated() -> None:
    ok, _ = permissions.path_allowed(
        "\\Imports\\foo", "  ,\\Imports  ,  ", None,
    )
    assert ok is True


def test_prefix_must_have_boundary() -> None:
    """`\\Imp` does not match `\\Imports\\foo` — same as deny test but
    on the allow side."""
    ok, _ = permissions.path_allowed(
        "\\Imports\\foo", "\\Imp", None,
    )
    assert ok is False


# --- path_allowed: traversal rejection ---------------------------------------


def test_path_with_dotdot_segment_rejected_unconditionally() -> None:
    ok, reason = permissions.path_allowed(
        "\\Sandbox\\..\\Secret", "\\Sandbox", None,
    )
    assert ok is False
    assert reason is not None
    assert ".." in reason


def test_path_with_dotdot_rejected_even_with_no_lists() -> None:
    """`..` is defense-in-depth — fires regardless of allow/deny config."""
    ok, _ = permissions.path_allowed("\\foo\\..\\bar", None, None)
    assert ok is False


def test_path_with_dotdot_via_forward_slash_rejected() -> None:
    ok, _ = permissions.path_allowed("/Sandbox/../Secret", None, None)
    assert ok is False


def test_dotdot_only_as_full_segment_rejected() -> None:
    """`..` as a literal segment is rejected; `..` inside a name is allowed
    (it's just a name, e.g. an entry literally named '..foo')."""
    ok, _ = permissions.path_allowed("\\Sandbox\\..foo\\bar", "\\Sandbox", None)
    assert ok is True


# --- name_allowed ------------------------------------------------------------


def test_name_allowed_accepts_normal_names() -> None:
    for name in ("Smith,John", "Invoice 2024-01.pdf", "Folder", "x"):
        ok, reason = permissions.name_allowed(name)
        assert ok is True, f"expected {name!r} to be allowed, got {reason}"


def test_name_allowed_rejects_empty() -> None:
    ok, reason = permissions.name_allowed("")
    assert ok is False
    assert reason is not None
    assert "empty" in reason.lower()


def test_name_allowed_rejects_whitespace_only() -> None:
    ok, reason = permissions.name_allowed("   ")
    assert ok is False
    assert reason is not None


def test_name_allowed_rejects_backslash() -> None:
    ok, reason = permissions.name_allowed("foo\\bar")
    assert ok is False
    assert "backslash" in reason.lower() or "\\\\" in reason or "'\\\\'" in reason


def test_name_allowed_rejects_forward_slash() -> None:
    ok, _ = permissions.name_allowed("foo/bar")
    assert ok is False


def test_name_allowed_rejects_null_byte() -> None:
    ok, _ = permissions.name_allowed("foo\x00bar")
    assert ok is False


def test_name_allowed_rejects_control_chars() -> None:
    ok, reason = permissions.name_allowed("foo\x01bar")
    assert ok is False
    assert reason is not None
    assert "control" in reason.lower()


def test_name_allowed_rejects_too_long() -> None:
    ok, reason = permissions.name_allowed("x" * 200)
    assert ok is False
    assert reason is not None
    assert "128" in reason


def test_name_allowed_accepts_exactly_128_chars() -> None:
    ok, _ = permissions.name_allowed("x" * 128)
    assert ok is True


# --- validate_page_range -----------------------------------------------------


def test_page_range_accepts_single_page() -> None:
    ok, _ = permissions.validate_page_range("1")
    assert ok is True


def test_page_range_accepts_comma_list() -> None:
    ok, _ = permissions.validate_page_range("1,2,3")
    assert ok is True


def test_page_range_accepts_range() -> None:
    ok, _ = permissions.validate_page_range("1-3")
    assert ok is True


def test_page_range_accepts_mixed() -> None:
    ok, _ = permissions.validate_page_range("1-3,5,7-9")
    assert ok is True


def test_page_range_rejects_empty() -> None:
    ok, _ = permissions.validate_page_range("")
    assert ok is False


def test_page_range_rejects_spaces() -> None:
    ok, reason = permissions.validate_page_range("1, 2, 3")
    assert ok is False
    assert reason is not None
    assert "space" in reason.lower()


def test_page_range_rejects_zero() -> None:
    ok, _ = permissions.validate_page_range("0,1,2")
    assert ok is False


def test_page_range_rejects_negative() -> None:
    ok, _ = permissions.validate_page_range("-1")
    assert ok is False


def test_page_range_rejects_leading_zero() -> None:
    ok, _ = permissions.validate_page_range("01")
    assert ok is False


def test_page_range_rejects_trailing_comma() -> None:
    ok, _ = permissions.validate_page_range("1,2,")
    assert ok is False


def test_page_range_rejects_descending_range() -> None:
    ok, reason = permissions.validate_page_range("5-3")
    assert ok is False
    assert reason is not None
    assert "ascending" in reason.lower() or "start" in reason.lower()


def test_page_range_rejects_non_string() -> None:
    ok, _ = permissions.validate_page_range(123)  # type: ignore[arg-type]
    assert ok is False


# --- tool_allowed -----------------------------------------------------------


def test_tool_allowlist_empty_means_all_allowed() -> None:
    ok, _ = permissions.tool_allowed("delete_entry", None)
    assert ok is True
    ok, _ = permissions.tool_allowed("delete_entry", "")
    assert ok is True


def test_tool_allowlist_filters() -> None:
    ok, _ = permissions.tool_allowed(
        "merge_fields", "merge_fields,merge_tags",
    )
    assert ok is True
    ok, reason = permissions.tool_allowed(
        "delete_entry", "merge_fields,merge_tags",
    )
    assert ok is False
    assert reason is not None
    assert "delete_entry" in reason


def test_parse_tool_allowlist_returns_set() -> None:
    parsed = permissions.parse_tool_allowlist("a, b, c")
    assert parsed == {"a", "b", "c"}


def test_parse_tool_allowlist_returns_none_when_empty() -> None:
    assert permissions.parse_tool_allowlist(None) is None
    assert permissions.parse_tool_allowlist("") is None
    assert permissions.parse_tool_allowlist("   ") is None
