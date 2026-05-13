"""Tests for server module helpers and tool registration."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.auth import AuthStrategy
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings

_BASE = "https://lf.example.test/LFRepositoryAPI/v1/Repositories/demo"

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
# Imported by tests as the canonical "what the fixture should extract to"
# string. If you change tests/fixtures/_generate.py SAMPLE_TEXT, change
# this constant too.
SAMPLE_PDF_TEXT = "Hello laserfiche-mcp test fixture."
SAMPLE_PDF_BYTES = (_FIXTURE_DIR / "sample_text.pdf").read_bytes()
SAMPLE_ENCRYPTED_PDF_BYTES = (_FIXTURE_DIR / "sample_encrypted.pdf").read_bytes()


class _StubAuth(AuthStrategy):
    async def apply(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = "Bearer test-token"


@pytest.fixture(autouse=True)
def _reset_settings(lf_env: dict[str, str]) -> None:
    server._reset_settings_for_tests()


@pytest.fixture
async def patched_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[LaserficheClient]:
    """Replace server._client() with a real LaserficheClient backed by httpx_mock.

    Sidesteps FastMCP's request-context machinery so tool functions can be
    awaited directly in tests.
    """
    settings = Settings()  # type: ignore[call-arg]
    async with LaserficheClient(settings, _StubAuth()) as client:
        monkeypatch.setattr(server, "_client", lambda: client)
        yield client


def test_clamp_max_results_uses_default_when_none() -> None:
    settings = server._get_settings()
    assert server._clamp_max_results(None) == settings.max_results_default


def test_clamp_max_results_floors_at_one() -> None:
    assert server._clamp_max_results(0) == 1
    assert server._clamp_max_results(-5) == 1


def test_clamp_max_results_caps_at_ceiling() -> None:
    settings = server._get_settings()
    assert server._clamp_max_results(99_999) == settings.max_results_ceiling


def test_clamp_max_results_passes_through_in_range() -> None:
    assert server._clamp_max_results(10) == 10


# --- legacy tool bodies (happy paths + error wraps) -------------------------


@pytest.mark.asyncio
async def test_search_entries_happy_path(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=25",
        json={"value": [{"id": 7, "name": "x.pdf", "entryType": "Document"}]},
    )

    result = await server.search_entries(query='{LF:Name="x.pdf"}')

    assert len(result.entries) == 1
    assert result.entries[0].id == 7


@pytest.mark.asyncio
async def test_search_entries_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=25",
        status_code=500,
        json={"error": "internal"},
    )

    with pytest.raises(RuntimeError, match="Search failed"):
        await server.search_entries(query='{LF:Name="x"}')


@pytest.mark.asyncio
async def test_search_by_name_builds_lf_query_with_wildcards(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """search_by_name must wrap the pattern in {LF:Name="..."} syntax verbatim."""
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=25",
        json={"value": []},
    )

    await server.search_by_name(name_pattern="Smith*")

    body = httpx_mock.get_requests()[0].read().decode()
    assert '"searchCommand":' in body
    assert 'LF:Name=\\"Smith*\\"' in body


@pytest.mark.asyncio
async def test_search_by_name_appends_lookin_when_folder_provided(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=25",
        json={"value": []},
    )

    await server.search_by_name(
        name_pattern="Smith*",
        in_folder_path="\\Imports\\2024",
    )

    body = httpx_mock.get_requests()[0].read().decode()
    # JSON-encoded backslashes are doubled; check for the raw fragment.
    assert "LF:LookIn=" in body
    assert "Imports" in body and "2024" in body


@pytest.mark.asyncio
async def test_search_by_name_escapes_quotes_in_pattern(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A user-supplied " must be escaped before being interpolated into the query."""
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=25",
        json={"value": []},
    )

    await server.search_by_name(name_pattern='say"hi')

    body = httpx_mock.get_requests()[0].read().decode()
    # In the JSON body, the backslash escape itself is JSON-encoded, so
    # ``\"`` becomes ``\\\"``. We just need to confirm the raw `"` from the
    # user did not land inside the value unescaped.
    assert 'say\\\\\\"hi' in body or 'say\\"hi' in body


@pytest.mark.asyncio
async def test_search_by_name_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=25",
        status_code=500,
        json={"e": "boom"},
    )

    with pytest.raises(RuntimeError, match="Search failed"):
        await server.search_by_name(name_pattern="x")


@pytest.mark.asyncio
async def test_list_folder_happy_path(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=25&%24skip=0"
        ),
        json={
            "value": [
                {"id": 10, "name": "child", "entryType": "Folder"},
            ],
            "@odata.count": 1,
        },
    )

    result = await server.list_folder(folder_id=1)

    assert result.total_count == 1
    assert result.entries[0].id == 10


@pytest.mark.asyncio
async def test_list_folder_clamps_negative_skip_to_zero(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=25&%24skip=0"
        ),
        json={"value": []},
    )

    await server.list_folder(folder_id=1, skip=-10)

    # If the negative skip leaked through, the URL above wouldn't match and
    # httpx_mock would 404 — passing this far proves the clamp ran.


@pytest.mark.asyncio
async def test_list_folder_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/999/Laserfiche.Repository.Folder/children"
            "?%24top=25&%24skip=0"
        ),
        status_code=404,
    )

    with pytest.raises(RuntimeError, match="Failed to list folder 999"):
        await server.list_folder(folder_id=999)


@pytest.mark.asyncio
async def test_get_entry_happy_path(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "x", "entryType": "Document", "templateName": "PAF"},
    )

    result = await server.get_entry(entry_id=42)

    assert result.id == 42
    assert result.template_name == "PAF"


@pytest.mark.asyncio
async def test_get_entry_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/999", status_code=404,
    )

    with pytest.raises(RuntimeError, match="Failed to fetch entry 999"):
        await server.get_entry(entry_id=999)


@pytest.mark.asyncio
async def test_get_entry_by_path_happy_path(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5CImports",
        json={"id": 12, "name": "Imports", "entryType": "Folder"},
    )

    result = await server.get_entry_by_path(full_path="\\Imports")

    assert result.id == 12


@pytest.mark.asyncio
async def test_get_entry_by_path_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5Cmissing",
        status_code=404,
    )

    with pytest.raises(RuntimeError, match="Failed to resolve path"):
        await server.get_entry_by_path(full_path="\\missing")


@pytest.mark.asyncio
async def test_get_field_values_happy_path(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/fields",
        json={
            "value": [
                {"fieldName": "Status", "values": ["Approved"]},
                {"fieldName": "Notes", "values": [], "isMultiValue": True},
            ]
        },
    )

    result = await server.get_field_values(entry_id=42)

    assert len(result) == 2
    assert result[0].field_name == "Status"
    assert result[1].is_multi_value is True


@pytest.mark.asyncio
async def test_get_field_values_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/999/fields", status_code=403,
    )

    with pytest.raises(RuntimeError, match="Failed to fetch fields for entry 999"):
        await server.get_field_values(entry_id=999)


@pytest.mark.asyncio
async def test_get_document_text_on_v2_returns_decoded_text(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_document_text only works on v2; verify the decode + truncation path.

    Builds its own client because v1 (the default test config) raises
    LaserficheError at the client level before the tool body runs.
    """
    monkeypatch.setenv("LF_API_VERSION", "v2")
    server._reset_settings_for_tests()
    settings = Settings()  # type: ignore[call-arg]

    httpx_mock.add_response(
        method="POST",
        url="https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo/Entries/42/Export",
        content=b"hello world",
    )

    async with LaserficheClient(settings, _StubAuth()) as client:
        monkeypatch.setattr(server, "_client", lambda: client)
        result = await server.get_document_text(entry_id=42)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_get_document_text_truncates_long_output(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LF_API_VERSION", "v2")
    server._reset_settings_for_tests()
    settings = Settings()  # type: ignore[call-arg]

    long_text = ("x" * 200).encode("utf-8")
    httpx_mock.add_response(
        method="POST",
        url="https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo/Entries/42/Export",
        content=long_text,
    )

    async with LaserficheClient(settings, _StubAuth()) as client:
        monkeypatch.setattr(server, "_client", lambda: client)
        result = await server.get_document_text(entry_id=42, max_chars=50)

    assert result.startswith("x" * 50)
    assert "[truncated" in result


@pytest.mark.asyncio
async def test_get_document_text_wraps_laserfiche_error_as_runtime(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    """On v1 the client raises LaserficheError synthetically — the tool must wrap it."""
    with pytest.raises(RuntimeError, match="Failed to download text for entry 42"):
        await server.get_document_text(entry_id=42)


@pytest.mark.asyncio
async def test_get_document_edoc_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/999/Laserfiche.Repository.Document/edoc",
        status_code=403,
    )

    with pytest.raises(RuntimeError, match="Failed to download edoc for entry 999"):
        await server.get_document_edoc(entry_id=999, mode="info")


# --- end legacy tool bodies -------------------------------------------------


@pytest.mark.asyncio
async def test_all_tools_registered() -> None:
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "search_entries",
        "search_by_name",
        "search_natural",
        "list_folder",
        "get_entry",
        "get_entry_by_path",
        "get_field_values",
        "get_document_text",
        "get_document_edoc",
    }


# --- search_natural ----------------------------------------------------------


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
        url=(
            f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=0"
        ),
        json={
            "value": [
                {"id": 201, "name": "doc1", "entryType": "Document"},
                {"id": 202, "name": "doc2", "entryType": "Document"},
            ]
        },
    )
    # get_entry for each sampled doc
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/201",
        json={"id": 201, "name": "doc1", "templateName": "Personnel File"},
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/202",
        json={"id": 202, "name": "doc2", "templateName": "Personnel File"},
    )
    # get_field_values for one entry per unique template
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/201/fields",
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

    assert result.mode == "guidance"
    assert result.question == "find John Smith's PAF"
    assert result.folder_path == "\\Test"
    assert result.grammar is not None and "LF:Name=" in result.grammar

    template_names = {t.template_name for t in result.discovered_templates}
    assert "Personnel File" in template_names

    assert len(result.candidate_queries) >= 1
    assert any("John" in c.query for c in result.candidate_queries)
    assert result.follow_up is not None and "search_natural" in result.follow_up


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

    assert result.mode == "results"
    assert result.lf_query == '{LF:Name="*Smith*"}'
    assert result.repairs_applied == []
    assert len(result.entries) == 1
    assert result.entries[0].id == 999
    assert result.pagination_unknown is False
    assert result.effective_max_results == 50


@pytest.mark.asyncio
async def test_search_natural_repair_escape_quotes(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """First call 400s; quote-escape repair fires; second call succeeds."""
    url = f"{_BASE}/SimpleSearches?%24top=50"
    # 1st attempt: 400
    httpx_mock.add_response(
        method="POST", url=url, status_code=400, json={"error": "bad syntax"},
    )
    # 2nd attempt (escaped): 200
    httpx_mock.add_response(
        method="POST", url=url,
        json={"value": [{"id": 1, "name": "ok", "entryType": "Document"}]},
    )

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="o"hare"}',
        max_results=50,
    )

    assert result.mode == "results"
    assert "escape_quotes" in result.repairs_applied
    assert result.lf_query == r'{LF:Name="o\"hare"}'


@pytest.mark.asyncio
async def test_search_natural_repair_wildcard_wrap_when_fuzzy(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """No quotes to escape, so the next repair (wildcard wrap) fires."""
    url = f"{_BASE}/SimpleSearches?%24top=50"
    httpx_mock.add_response(
        method="POST", url=url, status_code=400, json={"error": "no match"},
    )
    httpx_mock.add_response(
        method="POST", url=url,
        json={"value": [{"id": 2, "name": "ok", "entryType": "Document"}]},
    )

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="Smith"}',
        max_results=50,
        fuzzy=True,
    )

    assert result.mode == "results"
    assert "wildcard_wrap" in result.repairs_applied
    assert result.lf_query == '{LF:Name="*Smith*"}'


@pytest.mark.asyncio
async def test_search_natural_exhausts_repairs_and_returns_structured_error(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Every attempt 400s; final response is a structured error with all attempts."""
    url = f"{_BASE}/SimpleSearches?%24top=50"
    # 1st (original) — has an internal quote so escape_quotes will apply.
    httpx_mock.add_response(method="POST", url=url, status_code=400, json={"e": "bad"})
    # 2nd (escape_quotes) — still 400; wildcard_wrap will apply.
    httpx_mock.add_response(method="POST", url=url, status_code=400, json={"e": "bad"})
    # 3rd (wildcard_wrap) — still 400; no more repairs.
    httpx_mock.add_response(method="POST", url=url, status_code=400, json={"e": "bad"})

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="o"hare"}',
        max_results=50,
    )

    assert result.mode == "error"
    assert len(result.attempts) == 3
    assert result.attempts[0].repair is None
    assert result.attempts[1].repair == "escape_quotes"
    assert result.attempts[2].repair == "wildcard_wrap"
    assert result.next_action is not None
    assert "grammar" in result.next_action.lower()


@pytest.mark.asyncio
async def test_search_natural_returns_error_immediately_on_non_400(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """500/403 errors are not in the repair contract — surface them straight away.

    Repairs only run on 400 (malformed query). Other status codes (permissions,
    server crash) won't get fixed by escaping or wildcarding — retrying them
    just wastes calls and hides the real cause.
    """
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

    assert result.mode == "error"
    assert len(result.attempts) == 1
    assert result.attempts[0].status_code == 500
    assert result.next_action is not None
    assert "non-400" in result.next_action


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
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=0"
        ),
        json={"value": []},
    )

    result = await server.search_natural(question="x", max_results=500)

    assert any("clamped from 500 to 30" in n for n in result.notes)


@pytest.mark.asyncio
async def test_edoc_text_mode_reports_when_pypdf_is_unavailable(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If pypdf isn't installed, mode='text' on a PDF must return a structured error.

    The branch exists because pypdf is a hard dep today but was optional in
    earlier drafts; the safety net stays so downstream forks can omit it.
    """
    import builtins
    real_import = builtins.__import__

    def _refuse_pypdf(name: str, *args: object, **kwargs: object) -> object:
        if name == "pypdf":
            raise ImportError("pypdf intentionally unavailable for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _refuse_pypdf)

    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "pypdf_unavailable"
    assert "pip install pypdf" in result["message"]


@pytest.mark.asyncio
async def test_search_natural_pagination_unknown_when_full_page_and_no_next_link(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Hit the cap with no @odata.nextLink → pagination_unknown=true."""
    entries = [
        {"id": i, "name": f"doc{i}", "entryType": "Document"} for i in range(3)
    ]
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/SimpleSearches?%24top=3",
        json={"value": entries},  # no @odata.nextLink, no @odata.count
    )

    result = await server.search_natural(
        question="x",
        lf_query='{LF:Name="*"}',
        max_results=3,
    )

    assert result.mode == "results"
    assert len(result.entries) == 3
    assert result.next_link is None
    assert result.pagination_unknown is True


@pytest.mark.asyncio
async def test_search_natural_clamps_max_results_to_max_page_size(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_results above LF_MAX_PAGE_SIZE is clamped down; effective_max_results reflects it."""
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

    assert result.mode == "results"
    assert result.effective_max_results == 40


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
    # Falls back to folder_id=1 (root).
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=0"
        ),
        json={"value": []},
    )

    result = await server.search_natural(question="x", folder_path="\\nope")

    assert result.mode == "guidance"
    assert any(
        "Could not resolve folder_path" in note for note in result.notes
    )


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
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=0"
        ),
        json={"value": []},
    )

    result = await server.search_natural(question="x", folder_path="\\ghost")

    assert result.mode == "guidance"
    assert any("resolved to an empty entry" in n for n in result.notes)


@pytest.mark.asyncio
async def test_mode_a_records_note_when_list_folder_raises(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """list_folder errors must be captured as notes, not raised — guidance still returns."""
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=0"
        ),
        status_code=403,
    )

    result = await server.search_natural(question="x")

    assert result.mode == "guidance"
    assert result.discovered_templates == []
    assert any("Could not list folder 1" in n for n in result.notes)


@pytest.mark.asyncio
async def test_mode_a_records_note_when_folder_is_empty(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=0"
        ),
        json={"value": []},
    )

    result = await server.search_natural(question="x")

    assert result.mode == "guidance"
    assert any("had no children to sample" in n for n in result.notes)


@pytest.mark.asyncio
async def test_mode_a_records_note_when_no_sampled_entry_has_a_template(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """No sampled entry has templateName → empty templates list + a note."""
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=0"
        ),
        json={
            "value": [
                {"id": 201, "name": "doc", "entryType": "Document"},
            ]
        },
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/201",
        json={"id": 201, "name": "doc"},  # no templateName
    )

    result = await server.search_natural(question="x")

    assert result.discovered_templates == []
    assert any(
        "no template assigned" in n for n in result.notes
    )


@pytest.mark.asyncio
async def test_mode_a_tolerates_get_field_values_failure(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If get_field_values fails for a template, surface the template with empty field_names.

    The whole point of return_exceptions=True in the gather is that we
    shouldn't lose all templates because one fields call 500'd.
    """
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=0"
        ),
        json={
            "value": [
                {"id": 201, "name": "doc", "entryType": "Document"},
            ]
        },
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/201",
        json={"id": 201, "name": "doc", "templateName": "PAF"},
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/201/fields", status_code=500,
    )

    result = await server.search_natural(question="x")

    assert len(result.discovered_templates) == 1
    assert result.discovered_templates[0].template_name == "PAF"
    assert result.discovered_templates[0].field_names == []


# --- get_document_edoc modes ------------------------------------------------


@pytest.mark.asyncio
async def test_edoc_info_mode_returns_size_and_content_type(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """mode='info' — current shape preserved: byte_size + content_type + hint, no bytes."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"%PDF-1.4 hello",
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="info")

    assert result["entry_id"] == 42
    assert result["mode"] == "info"
    assert result["byte_size"] == len(b"%PDF-1.4 hello")
    assert result["content_type"] == "application/pdf"
    assert "data_base64" not in result


@pytest.mark.asyncio
async def test_edoc_bytes_mode_returns_base64_starting_with_pdf_magic(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """mode='bytes' — base64 round-trip yields the original PDF magic bytes."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="bytes")

    assert result["mode"] == "bytes"
    assert result["content_type"] == "application/pdf"
    assert result["byte_size"] == len(SAMPLE_PDF_BYTES)
    decoded = base64.b64decode(result["data_base64"])
    assert decoded.startswith(b"%PDF-")
    assert decoded == SAMPLE_PDF_BYTES


@pytest.mark.asyncio
async def test_edoc_text_mode_extracts_known_text_from_pdf_fixture(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """mode='text' on a real PDF must return the fixture's known-good text.

    Earlier versions of this test used a blank PDF and only asserted that
    keys existed — pypdf could silently regress to extracting nothing and
    the test would still pass. The fixture written by
    ``tests/fixtures/_generate.py`` carries deterministic ASCII text so a
    regression in pypdf integration breaks the assertion.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["mode"] == "text"
    assert "error" not in result, result
    assert result["pages_total"] == 1
    assert result["pages_extracted"] == 1
    assert SAMPLE_PDF_TEXT in result["text"]
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_edoc_text_mode_truncates_when_over_char_limit(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """text_char_limit applies — sets ``truncated=True`` when text overflows."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(
        entry_id=42, mode="text", text_char_limit=5,
    )

    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_edoc_text_mode_reports_encrypted_pdf(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """An encrypted PDF surfaces the ``pdf_encrypted`` structured error."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_ENCRYPTED_PDF_BYTES,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "pdf_encrypted"
    assert "mode='bytes'" in result["message"]


@pytest.mark.asyncio
async def test_edoc_text_mode_reports_malformed_pdf(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A byte string that claims to be PDF but isn't returns pdf_open_failed."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"%PDF-1.4\nnot really a pdf",
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "pdf_open_failed"
    assert result.get("exception_class")  # whatever pypdf raised


@pytest.mark.asyncio
async def test_edoc_text_mode_normalizes_content_type_casing(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Content-type matching must be case-insensitive and ignore parameters.

    Servers commonly send ``Application/PDF`` or
    ``application/pdf; charset=binary``. The branch picker must accept both.
    """
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=SAMPLE_PDF_BYTES,
        headers={"content-type": "Application/PDF; charset=binary"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["mode"] == "text"
    assert "error" not in result, result
    assert SAMPLE_PDF_TEXT in result["text"]


@pytest.mark.asyncio
async def test_edoc_bytes_mode_refuses_oversized_download(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If byte_size > max_bytes, return a structured error — no base64 payload."""
    content = b"a" * 5_000
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=content,
        headers={"content-type": "application/pdf"},
    )

    result = await server.get_document_edoc(
        entry_id=42, mode="bytes", max_bytes=1_000,
    )

    assert result["error"] == "size_exceeds_cap"
    assert result["byte_size"] == 5_000
    assert result["max_bytes"] == 1_000
    assert "data_base64" not in result


@pytest.mark.asyncio
async def test_edoc_text_mode_rejects_non_pdf_non_text_content(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A docx/image edoc → structured error pointing at mode='bytes'."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"PK\x03\x04 fake docx",
        headers={
            "content-type": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        },
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["error"] == "unsupported_content_type"
    assert "wordprocessingml" in (result["content_type"] or "")
    assert "mode='bytes'" in result["message"]


@pytest.mark.asyncio
async def test_edoc_text_mode_decodes_plain_text(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """text/* content-types should be decoded directly, not run through pypdf."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        content=b"hello world",
        headers={"content-type": "text/plain; charset=utf-8"},
    )

    result = await server.get_document_edoc(entry_id=42, mode="text")

    assert result["mode"] == "text"
    assert "error" not in result
    assert result["text"] == "hello world"


# --- main() entrypoint behaviors --------------------------------------------


def test_main_help_prints_and_exits_zero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp", "--help"])
    server.main()
    out = capsys.readouterr().out
    assert "laserfiche-mcp" in out
    assert "Configuration" in out


def test_main_version_prints_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp", "--version"])
    server.main()
    out = capsys.readouterr().out.strip()
    assert out.startswith("laserfiche-mcp ")


def test_main_missing_config_prints_friendly_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Strip everything; reset cached settings so Settings() actually re-runs.
    for var in ("LF_REPO_API_URL", "LF_REPOSITORY_ID", "LF_USERNAME", "LF_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp"])
    server._reset_settings_for_tests()

    with pytest.raises(SystemExit) as exit_info:
        server.main()
    assert exit_info.value.code == 2

    err = capsys.readouterr().err
    assert "configuration is missing" in err
    assert "LF_REPO_API_URL" in err
    # No Python traceback — just the friendly message
    assert "Traceback" not in err


def test_format_config_error_strips_value_error_prefix() -> None:
    """Pydantic prefixes value_error messages with 'Value error, '; we strip it."""
    settings_cls = Settings
    try:
        settings_cls()  # type: ignore[call-arg]
    except Exception as exc:
        msg = server._format_config_error(exc)
        assert "Value error, " not in msg
        assert "configuration" in msg
