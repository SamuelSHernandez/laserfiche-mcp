"""Tests for ``tools/_helpers.py``'s pure functions."""

from __future__ import annotations

from laserfiche_mcp.tools._helpers import entry_path


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
