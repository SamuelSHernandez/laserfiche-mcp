"""Tests for the permissions module (path scope + tool allowlist)."""

from __future__ import annotations

import pytest

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
