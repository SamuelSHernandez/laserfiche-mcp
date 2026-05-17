"""Tests for ``tools/natural_search.py`` — guidance + execute modes with repair.

Covers ``search_natural``'s two modes (Mode A guidance, Mode B execute),
the two query-repair strategies (escape unescaped quotes, wildcard-wrap
Name= values), pagination-unknown sentinel, max_results clamping, and
the ``_sample_folder_templates`` edge cases (folder 404s, empty folders,
sampled entries without templates, individual fetch failures).
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

# --- Mode A: guidance --------------------------------------------------------


@pytest.mark.asyncio
async def test_search_natural_mode_a_returns_guidance(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Mode A samples folder_path, surfaces templates, returns candidate queries."""
    # Resolve folder
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5CTest",
        json={"id": 100, "name": "Test", "entryType": "Folder"},
    )
    # List children
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children?%24top=10&%24skip=0"),
        json={
            "value": [
                {"id": 201, "name": "doc1", "entryType": "Document"},
                {"id": 202, "name": "doc2", "entryType": "Document"},
            ]
        },
    )
    # get_entry for each sampled doc
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/201",
        json={"id": 201, "name": "doc1", "templateName": "Personnel File"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/202",
        json={"id": 202, "name": "doc2", "templateName": "Personnel File"},
    )
    # get_field_values for one entry per unique template
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/201/fields",
        json={
            "value": [
                {"fieldName": "Person Name", "values": []},
                {"fieldName": "Status", "values": []},
            ]
        },
    )

    result = await server.search_natural(
        question="find John Smith's PAF",
        folder_path="\\Test",
    )

    assert result["mode"] == "guidance"
    assert result["question"] == "find John Smith's PAF"
    assert result["folder_path"] == "\\Test"
    assert result["grammar"] is not None and "LF:Name=" in result["grammar"]

    template_names = {t["template_name"] for t in result["discovered_templates"]}
    assert "Personnel File" in template_names

    assert len(result["candidate_queries"]) >= 1
    assert any("John" in c["query"] for c in result["candidate_queries"])
    assert result["follow_up"] is not None and "search_natural" in result["follow_up"]


@pytest.mark.asyncio
async def test_search_natural_mode_a_notes_when_max_results_is_clamped(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode A guidance surfaces a note when the caller's max_results was clamped."""
    monkeypatch.setenv("LF_MAX_PAGE_SIZE", "30")
    server._reset_settings_for_tests()
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=10&%24skip=0"),
        json={"value": []},
    )

    result = await server.search_natural(question="x", max_results=500)

    assert any("clamped from 500 to 30" in n for n in result["notes"])


# --- Mode B: execute with repair --------------------------------------------


@pytest.mark.asyncio
async def test_search_natural_mode_b_executes_and_returns_results(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=50",
        json={
            "value": [
                {"id": 999, "name": "found.pdf", "entryType": "Document"},
            ]
        },
    )

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="*Smith*"}',
        max_results=50,
    )

    assert result["mode"] == "results"
    assert result["lf_query"] == '{LF:Name="*Smith*"}'
    assert result["repairs_applied"] == []
    assert len(result["entries"]) == 1
    assert result["entries"][0]["id"] == 999
    assert result["pagination_unknown"] is False
    assert result["effective_max_results"] == 50


@pytest.mark.asyncio
async def test_search_natural_repair_escape_quotes(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """First call 400s; quote-escape repair fires; second call succeeds."""
    url = f"{_BASE}/SimpleSearches?%24top=50"
    httpx_mock.add_response(
        method="POST",
        url=url,
        status_code=400,
        json={"error": "bad syntax"},
    )
    httpx_mock.add_response(
        method="POST",
        url=url,
        json={"value": [{"id": 1, "name": "ok", "entryType": "Document"}]},
    )

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="o"hare"}',
        max_results=50,
    )

    assert result["mode"] == "results"
    assert "escape_quotes" in result["repairs_applied"]
    assert result["lf_query"] == r'{LF:Name="o\"hare"}'


@pytest.mark.asyncio
async def test_search_natural_repair_wildcard_wrap_when_fuzzy(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """No quotes to escape, so the next repair (wildcard wrap) fires."""
    url = f"{_BASE}/SimpleSearches?%24top=50"
    httpx_mock.add_response(
        method="POST",
        url=url,
        status_code=400,
        json={"error": "no match"},
    )
    httpx_mock.add_response(
        method="POST",
        url=url,
        json={"value": [{"id": 2, "name": "ok", "entryType": "Document"}]},
    )

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="Smith"}',
        max_results=50,
        fuzzy=True,
    )

    assert result["mode"] == "results"
    assert "wildcard_wrap" in result["repairs_applied"]
    assert result["lf_query"] == '{LF:Name="*Smith*"}'


@pytest.mark.asyncio
async def test_search_natural_exhausts_repairs_and_returns_structured_error(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Every attempt 400s; final response is a structured error with all attempts."""
    url = f"{_BASE}/SimpleSearches?%24top=50"
    httpx_mock.add_response(method="POST", url=url, status_code=400, json={"e": "bad"})
    httpx_mock.add_response(method="POST", url=url, status_code=400, json={"e": "bad"})
    httpx_mock.add_response(method="POST", url=url, status_code=400, json={"e": "bad"})

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="o"hare"}',
        max_results=50,
    )

    assert result["mode"] == "error"
    assert len(result["attempts"]) == 3
    assert result["attempts"][0]["repair"] is None
    assert result["attempts"][1]["repair"] == "escape_quotes"
    assert result["attempts"][2]["repair"] == "wildcard_wrap"
    assert result["next_action"] is not None
    assert "grammar" in result["next_action"].lower()


@pytest.mark.asyncio
async def test_search_natural_returns_error_immediately_on_non_400(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """500/403 errors are not in the repair contract — surface them straight away."""
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=50",
        status_code=500,
        json={"error": "internal"},
    )

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="*"}',
        max_results=50,
    )

    assert result["mode"] == "error"
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["status_code"] == 500
    assert result["next_action"] is not None
    assert "non-400" in result["next_action"]


@pytest.mark.asyncio
async def test_search_natural_pagination_unknown_when_full_page_and_no_next_link(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Hit the cap with no @odata.nextLink → pagination_unknown=true."""
    entries = [{"id": i, "name": f"doc{i}", "entryType": "Document"} for i in range(3)]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=3",
        json={"value": entries},
    )

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="*"}',
        max_results=3,
    )

    assert result["mode"] == "results"
    assert len(result["entries"]) == 3
    assert result["next_link"] is None
    assert result["pagination_unknown"] is True


@pytest.mark.asyncio
async def test_search_natural_clamps_max_results_to_max_page_size(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_results above LF_MAX_PAGE_SIZE is clamped down."""
    monkeypatch.setenv("LF_MAX_PAGE_SIZE", "40")
    server._reset_settings_for_tests()

    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=40",
        json={"value": []},
    )

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="*"}',
        max_results=500,
    )

    assert result["mode"] == "results"
    assert result["effective_max_results"] == 40


# --- _sample_folder_templates edge cases ------------------------------------


@pytest.mark.asyncio
async def test_mode_a_falls_back_to_root_when_folder_path_404s(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If folder_path doesn't resolve, sampling falls back to root + records a note."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5Cnope",
        status_code=404,
    )
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=10&%24skip=0"),
        json={"value": []},
    )

    result = await server.search_natural(question="x", folder_path="\\nope")

    assert result["mode"] == "guidance"
    assert any("Could not resolve folder_path" in note for note in result["notes"])


@pytest.mark.asyncio
async def test_mode_a_falls_back_to_root_when_path_resolves_to_empty_entry(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A path that resolves but returns id=0 (server's "not found" sentinel) → root fallback."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5Cghost",
        json={"id": 0, "name": "", "entryType": "Unknown"},
    )
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=10&%24skip=0"),
        json={"value": []},
    )

    result = await server.search_natural(question="x", folder_path="\\ghost")

    assert result["mode"] == "guidance"
    assert any("resolved to an empty entry" in n for n in result["notes"])


@pytest.mark.asyncio
async def test_mode_a_records_note_when_list_folder_raises(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """list_folder errors must be captured as notes, not raised — guidance still returns."""
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=10&%24skip=0"),
        status_code=403,
    )

    result = await server.search_natural(question="x")

    assert result["mode"] == "guidance"
    assert result["discovered_templates"] == []
    assert any("Could not list folder 1" in n for n in result["notes"])


@pytest.mark.asyncio
async def test_mode_a_records_note_when_folder_is_empty(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=10&%24skip=0"),
        json={"value": []},
    )

    result = await server.search_natural(question="x")

    assert result["mode"] == "guidance"
    assert any("had no children to sample" in n for n in result["notes"])


@pytest.mark.asyncio
async def test_mode_a_records_note_when_no_sampled_entry_has_a_template(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """No sampled entry has templateName → empty templates list + a note."""
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=10&%24skip=0"),
        json={
            "value": [
                {"id": 201, "name": "doc", "entryType": "Document"},
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/201",
        json={"id": 201, "name": "doc"},  # no templateName
    )

    result = await server.search_natural(question="x")

    assert result["discovered_templates"] == []
    assert any("no template assigned" in n for n in result["notes"])


@pytest.mark.asyncio
async def test_mode_a_tolerates_get_field_values_failure(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If get_field_values fails for a template, surface it with empty field_names.

    The whole point of return_exceptions=True in the gather is that we
    shouldn't lose all templates because one fields call 500'd.
    """
    httpx_mock.add_response(
        method="GET",
        url=(f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=10&%24skip=0"),
        json={
            "value": [
                {"id": 201, "name": "doc", "entryType": "Document"},
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/201",
        json={"id": 201, "name": "doc", "templateName": "PAF"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/201/fields",
        status_code=500,
    )

    result = await server.search_natural(question="x")

    assert len(result["discovered_templates"]) == 1
    assert result["discovered_templates"][0]["template_name"] == "PAF"
    assert result["discovered_templates"][0]["field_names"] == []
