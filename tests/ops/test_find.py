"""Tests for ``ops/find.py`` — in-document search."""

from __future__ import annotations

import re

import pytest

from laserfiche_mcp.ops.extract import ExtractedText
from laserfiche_mcp.ops.find import find_in_text


def _paginated(*pages: str) -> ExtractedText:
    return ExtractedText(text="\n".join(pages), backend="test", pages=list(pages))


def _flat(text: str) -> ExtractedText:
    return ExtractedText(text=text, backend="test")


def test_literal_search_is_the_default() -> None:
    """A phrase containing regex metacharacters must not become a pattern."""
    doc = _flat("total is 4.5 percent")

    hits, total = find_in_text(doc, "4.5")

    assert total == 1
    # If this were treated as a regex, '4x5' would also match.
    assert find_in_text(_flat("4x5"), "4.5")[1] == 0


def test_regex_mode_when_asked() -> None:
    doc = _flat("invoice 12345 and invoice 67890")

    hits, total = find_in_text(doc, r"invoice \d+", regex=True)

    assert total == 2
    assert hits[0].matched == "invoice 12345"


def test_matches_carry_page_numbers_for_paginated_documents() -> None:
    doc = _paginated("nothing here", "the unpaid balance is due", "nor here")

    hits, total = find_in_text(doc, "unpaid balance")

    assert total == 1
    assert hits[0].page == 2


def test_unpaginated_documents_report_no_page() -> None:
    hits, _ = find_in_text(_flat("unpaid balance"), "unpaid balance")

    assert hits[0].page is None
    assert hits[0].line == 1


def test_line_numbers_are_one_based_within_the_unit() -> None:
    doc = _flat("alpha\nbeta\ngamma\ndelta")

    hits, _ = find_in_text(doc, "gamma")

    assert hits[0].line == 3


def test_ignore_case_by_default_and_respects_case_sensitive() -> None:
    doc = _flat("Termination for Convenience")

    assert find_in_text(doc, "termination")[1] == 1
    assert find_in_text(doc, "termination", ignore_case=False)[1] == 0


def test_total_counts_every_match_even_when_the_list_is_capped() -> None:
    """The distinction between '3 hits' and 'the first 3 of 900' decides
    whether the search was specific enough."""
    doc = _flat(" ".join(["needle"] * 50))

    hits, total = find_in_text(doc, "needle", max_matches=3)

    assert len(hits) == 3
    assert total == 50


def test_context_window_is_bounded_and_marks_elision() -> None:
    doc = _flat("x" * 500 + " needle " + "y" * 500)

    hits, _ = find_in_text(doc, "needle", context_chars=60)

    context = hits[0].context
    assert "needle" in context
    assert context.startswith("…")
    assert context.endswith("…")
    assert len(context) <= 62  # window plus the two ellipsis characters


def test_context_keeps_the_match_when_the_match_exceeds_the_window() -> None:
    """Truncating the matched text itself would defeat the point."""
    long_match = "z" * 100
    doc = _flat(f"before {long_match} after")

    hits, _ = find_in_text(doc, long_match, context_chars=40)

    assert hits[0].matched == long_match


def test_newlines_are_flattened_so_one_match_is_one_line() -> None:
    doc = _flat("first\nneedle\nlast")

    assert "\n" not in find_in_text(doc, "needle")[0][0].context


def test_no_matches_returns_empty_and_zero() -> None:
    hits, total = find_in_text(_flat("nothing relevant"), "absent")

    assert hits == []
    assert total == 0


def test_invalid_regex_propagates_for_the_caller_to_surface() -> None:
    with pytest.raises(re.error):
        find_in_text(_flat("text"), "(unclosed", regex=True)
