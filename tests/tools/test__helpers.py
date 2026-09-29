"""Tests for ``tools/_helpers.py``'s pure functions."""

from __future__ import annotations

from laserfiche_mcp import confirmation
from laserfiche_mcp.tools._helpers import entry_path, verify_confirmation_token


def test_entry_path_returns_none_for_none_entry() -> None:
    assert entry_path(None) is None


def test_entry_path_returns_none_when_key_absent() -> None:
    assert entry_path({"id": 1, "name": "x"}) is None


def test_entry_path_reads_camelcase() -> None:
    assert entry_path({"fullPath": "\\Imports\\x"}) == "\\Imports\\x"


def test_entry_path_reads_pascalcase() -> None:
    assert entry_path({"FullPath": "\\Imports\\x"}) == "\\Imports\\x"


def test_entry_path_distinguishes_absent_from_present_but_empty() -> None:
    """Regression: `entry.get("fullPath") or entry.get("FullPath")` used to
    collapse a present-but-empty fullPath to None, which
    permissions.path_allowed() reads as "unknown, can't enforce" — letting
    a write through a fence it should have been checked against."""
    assert entry_path({"fullPath": ""}) == ""
    assert entry_path({"FullPath": ""}) == ""


# --- verify_confirmation_token: the shared token verify/reject skeleton ----
# every destructive multiplex tool (rename_entry, move_entry, delete_entry,
# delete_edoc, delete_pages) now calls instead of hand-rolling.


def test_verify_confirmation_token_returns_none_on_success() -> None:
    token = confirmation.create_token("delete_entry", 42, "Doc")
    assert verify_confirmation_token(token, "delete_entry", 42, "Doc") is None


def test_verify_confirmation_token_returns_structured_error_on_failure() -> None:
    result = verify_confirmation_token("not-a-real-token", "delete_entry", 42, "Doc")
    assert result is not None
    assert result["mode"] == "error"
    assert result["error"] == "invalid_confirmation_token"
    assert result["entry_id"] == 42


def test_verify_confirmation_token_checks_bound_params() -> None:
    token = confirmation.create_token(
        "delete_pages", 42, "Doc", params={"page_range": "1-3", "page_count": 10}
    )
    # Same token, different params than what was previewed — must reject.
    result = verify_confirmation_token(
        token,
        "delete_pages",
        42,
        "Doc",
        params={"page_range": "1-9999", "page_count": 10},
    )
    assert result is not None
    assert result["error"] == "invalid_confirmation_token"
