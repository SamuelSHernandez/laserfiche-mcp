"""Tests for the pure helpers in laserfiche_mcp.search."""

from __future__ import annotations

from laserfiche_mcp.models import TemplateHint
from laserfiche_mcp.search import (
    LF_GRAMMAR_REFERENCE,
    build_candidate_queries,
    extract_keywords,
    repair_escape_quotes,
    repair_wildcard_name,
)

# --- repair_escape_quotes ---------------------------------------------------


def test_escape_quotes_returns_none_when_no_change_needed() -> None:
    assert repair_escape_quotes('{LF:Name="Smith"}') is None
    assert repair_escape_quotes('{[Personnel]:[Last Name]="Smith"}') is None


def test_escape_quotes_handles_internal_quote() -> None:
    out = repair_escape_quotes('{LF:Name="say "hi" smith"}')
    assert out == r'{LF:Name="say \"hi\" smith"}'


def test_escape_quotes_leaves_already_escaped_quotes_alone() -> None:
    original = r'{LF:Name="say \"hi\" smith"}'
    assert repair_escape_quotes(original) is None


def test_escape_quotes_handles_and_combinator() -> None:
    out = repair_escape_quotes('{LF:Name="o"hare"} & {LF:LookIn="\\Imports"}')
    assert out == r'{LF:Name="o\"hare"} & {LF:LookIn="\Imports"}'


# --- repair_wildcard_name ---------------------------------------------------


def test_wildcard_wrap_adds_stars_to_bare_value() -> None:
    assert repair_wildcard_name('{LF:Name="Smith"}') == '{LF:Name="*Smith*"}'


def test_wildcard_wrap_returns_none_when_wildcards_already_present() -> None:
    assert repair_wildcard_name('{LF:Name="Smith*"}') is None
    assert repair_wildcard_name('{LF:Name="*Smith"}') is None
    assert repair_wildcard_name('{LF:Name="*Smi?h*"}') is None


def test_wildcard_wrap_skips_empty_value() -> None:
    assert repair_wildcard_name('{LF:Name=""}') is None


def test_wildcard_wrap_handles_multiple_name_clauses() -> None:
    out = repair_wildcard_name('{LF:Name="Smith"} & {LF:Name="John"}')
    assert out == '{LF:Name="*Smith*"} & {LF:Name="*John*"}'


def test_wildcard_wrap_leaves_template_clauses_alone() -> None:
    """Only LF:Name clauses get wildcard wrapping — template-field clauses don't."""
    original = '{[Personnel]:[Last Name]="Smith"}'
    assert repair_wildcard_name(original) is None


# --- extract_keywords -------------------------------------------------------


def test_extract_keywords_picks_capitalized_tokens() -> None:
    assert extract_keywords("find John Smith's PAF") == ["John", "Smith", "PAF"]


def test_extract_keywords_drops_stop_words() -> None:
    out = extract_keywords("Find The Onboarding form")
    # "Find" and "The" are stop words; "Onboarding" survives.
    assert "Find" not in out
    assert "The" not in out
    assert "Onboarding" in out


def test_extract_keywords_dedupes() -> None:
    assert extract_keywords("Smith Smith Jones") == ["Smith", "Jones"]


def test_extract_keywords_empty_for_lowercase_only() -> None:
    assert extract_keywords("show me all the documents") == []


# --- build_candidate_queries ------------------------------------------------


def test_build_candidates_includes_fuzzy_name() -> None:
    out = build_candidate_queries(
        "find John Smith's PAF",
        folder_path=None,
        templates=[],
    )
    queries = [c.query for c in out]
    assert any('{LF:Name="*John*"}' in q for q in queries)


def test_build_candidates_uses_path_scope_when_provided() -> None:
    out = build_candidate_queries(
        "find John Smith",
        folder_path="\\ISE Records\\S",
        templates=[],
    )
    assert all('{LF:LookIn="\\ISE Records\\S"}' in c.query for c in out)


def test_build_candidates_uses_template_when_available() -> None:
    out = build_candidate_queries(
        "find John Smith",
        folder_path=None,
        templates=[
            TemplateHint(
                template_name="Personnel File",
                field_names=["Person Name", "Status"],
            )
        ],
    )
    queries = [c.query for c in out]
    assert any("[Personnel File]" in q and "[Person Name]" in q for q in queries)


def test_build_candidates_returns_at_most_three() -> None:
    out = build_candidate_queries(
        "Alpha Beta Gamma Delta Epsilon",
        folder_path="\\X",
        templates=[
            TemplateHint(template_name="Tpl", field_names=["Name"]),
        ],
    )
    assert len(out) <= 3


def test_build_candidates_never_empty_even_without_keywords() -> None:
    out = build_candidate_queries(
        "show me everything",
        folder_path=None,
        templates=[],
    )
    assert len(out) >= 1


# --- grammar reference ------------------------------------------------------


def test_grammar_reference_has_required_examples() -> None:
    """Sanity check that the grammar string covers what we tell the LLM it does."""
    assert "LF:Name=" in LF_GRAMMAR_REFERENCE
    assert "LF:LookIn=" in LF_GRAMMAR_REFERENCE
    assert "(AND)" in LF_GRAMMAR_REFERENCE
    assert "(OR)" in LF_GRAMMAR_REFERENCE
