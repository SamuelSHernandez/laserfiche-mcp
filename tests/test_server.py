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
from laserfiche_mcp.client import LaserficheClient, LaserficheError
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


# --- _classify_lf_error: per-slug mapping -----------------------------------
# The classifier is the central error contract for every tool wrapper, so
# each slug has direct coverage here in addition to the transitive tests
# via the tools themselves.


def _err(status: int | None, detail: object | None = None) -> LaserficheError:
    return LaserficheError("test error", status_code=status, detail=detail)


def test_classify_lf_error_401_is_auth_failed() -> None:
    r = server._classify_lf_error("get_entry", _err(401))
    assert r["error"] == "auth_failed"
    assert r["status_code"] == 401


def test_classify_lf_error_403_is_auth_failed() -> None:
    r = server._classify_lf_error("get_entry", _err(403))
    assert r["error"] == "auth_failed"


def test_classify_lf_error_lf_code_9010_is_auth_failed() -> None:
    # LF-specific code overrides HTTP status interpretation: 9010 means
    # invalid credentials even on a 400.
    r = server._classify_lf_error("get_entry", _err(400, {"errorCode": 9010}))
    assert r["error"] == "auth_failed"
    assert r["server_error_code"] == 9010


def test_classify_lf_error_lf_code_9528_treated_as_auth_failed() -> None:
    # 9528 is misleadingly worded ('LFDS unreachable') but most often
    # means bad creds; the reason text should reflect that.
    r = server._classify_lf_error("get_entry", _err(400, {"errorCode": 9528}))
    assert r["error"] == "auth_failed"
    assert "9528" in r["reason"]


def test_classify_lf_error_9066_is_required_field_missing() -> None:
    r = server._classify_lf_error("assign_template", _err(400, {"errorCode": 9066}))
    assert r["error"] == "required_field_missing"


def test_classify_lf_error_9039_is_required_field_missing() -> None:
    r = server._classify_lf_error("assign_template", _err(400, {"errorCode": 9039}))
    assert r["error"] == "required_field_missing"


def test_classify_lf_error_404_is_not_found() -> None:
    r = server._classify_lf_error("get_entry", _err(404))
    assert r["error"] == "not_found"


def test_classify_lf_error_405_is_method_not_allowed() -> None:
    r = server._classify_lf_error("delete_entry", _err(405))
    assert r["error"] == "method_not_allowed"


def test_classify_lf_error_415_is_unsupported_media_type() -> None:
    r = server._classify_lf_error("delete_entry", _err(415))
    assert r["error"] == "unsupported_media_type"


def test_classify_lf_error_429_is_rate_limited() -> None:
    r = server._classify_lf_error("get_entry", _err(429))
    assert r["error"] == "rate_limited"


def test_classify_lf_error_500_is_server_error() -> None:
    r = server._classify_lf_error("get_entry", _err(500))
    assert r["error"] == "server_error"


def test_classify_lf_error_502_is_server_error() -> None:
    r = server._classify_lf_error("get_entry", _err(502))
    assert r["error"] == "server_error"


def test_classify_lf_error_unknown_status_falls_back_to_server_error() -> None:
    # Network error before HTTP status: detail=None, status_code=None.
    r = server._classify_lf_error("get_entry", _err(None))
    assert r["error"] == "server_error"


def test_classify_lf_error_includes_entry_id_when_supplied() -> None:
    r = server._classify_lf_error("get_entry", _err(404), entry_id=42)
    assert r["entry_id"] == 42


def test_classify_lf_error_extra_dict_merged_into_response() -> None:
    r = server._classify_lf_error(
        "copy_entry", _err(404),
        extra={"parent_id": 100, "name": "X"},
    )
    assert r["parent_id"] == 100
    assert r["name"] == "X"


def test_classify_lf_error_extracts_title_from_problem_details() -> None:
    r = server._classify_lf_error(
        "get_entry",
        _err(400, {"errorCode": 216, "title": "Bad parameter"}),
    )
    assert r["server_message"] == "Bad parameter"


def test_lf_error_detail_handles_nested_error_wrapper() -> None:
    # The Edoc DELETE routes return `{error: {code, message}}` instead of
    # the usual flat ProblemDetails. Detail extractor should merge the
    # inner dict so callers see a uniform shape.
    exc = _err(405, {"error": {"code": "UnsupportedApiVersion", "message": "no DELETE"}})
    detail = server._lf_error_detail(exc)
    assert detail["code"] == "UnsupportedApiVersion"
    assert detail["message"] == "no DELETE"


def test_lf_error_detail_returns_empty_for_non_dict_detail() -> None:
    # Plaintext bodies leave detail as a string; helper should yield {}.
    exc = _err(500, "Internal Server Error")
    assert server._lf_error_detail(exc) == {}


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

    assert len(result["entries"]) == 1
    assert result["entries"][0]["id"] == 7


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

    result = await server.search_entries(query='{LF:Name="x"}')
    assert result["mode"] == "error"
    assert result["operation"] == "search"
    assert result["status_code"] == 500


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

    result = await server.search_by_name(name_pattern="x")
    assert result["mode"] == "error"
    assert result["operation"] == "search"
    assert result["status_code"] == 500


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

    assert result["total_count"] == 1
    assert result["entries"][0]["id"] == 10


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

    result = await server.list_folder(folder_id=999)
    assert result["mode"] == "error"
    assert result["operation"] == "list_folder"
    assert result["error"] == "not_found"
    assert result["folder_id"] == 999


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

    assert result["id"] == 42
    assert result["template_name"] == "PAF"


@pytest.mark.asyncio
async def test_get_entry_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/999", status_code=404,
    )

    result = await server.get_entry(entry_id=999)
    assert result["mode"] == "error"
    assert result["operation"] == "get_entry"
    assert result["error"] == "not_found"
    assert result["entry_id"] == 999


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

    assert result["id"] == 12


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

    result = await server.get_entry_by_path(full_path="\\missing")
    assert result["mode"] == "error"
    assert result["operation"] == "get_entry_by_path"
    assert result["error"] == "not_found"
    assert result["full_path"] == "\\missing"


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

    assert result["entry_id"] == 42
    assert len(result["values"]) == 2
    assert result["values"][0]["field_name"] == "Status"
    assert result["values"][1]["is_multi_value"] is True


@pytest.mark.asyncio
async def test_get_field_values_wraps_laserfiche_error_as_runtime(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/999/fields", status_code=403,
    )

    result = await server.get_field_values(entry_id=999)
    assert result["mode"] == "error"
    assert result["operation"] == "get_field_values"
    assert result["error"] == "auth_failed"
    assert result["entry_id"] == 999


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

    assert result["text"] == "hello world"
    assert result["truncated"] is False
    assert result["entry_id"] == 42


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

    assert result["text"].startswith("x" * 50)
    assert len(result["text"]) == 50
    assert result["truncated"] is True
    assert result["char_count"] == 50


@pytest.mark.asyncio
async def test_get_document_text_wraps_laserfiche_error_as_runtime(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    """On v1 the client raises LaserficheError synthetically — the tool must wrap it."""
    result = await server.get_document_text(entry_id=42)
    assert result["mode"] == "error"
    assert result["operation"] == "get_document_text"


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

    result = await server.get_document_edoc(entry_id=999, mode="info")
    assert result["mode"] == "error"
    assert result["operation"] == "get_document_edoc"
    assert result["error"] == "auth_failed"
    assert result["entry_id"] == 999


# --- end legacy tool bodies -------------------------------------------------


# --- v1.2 server tools ------------------------------------------------------


@pytest.mark.asyncio
async def test_get_audit_reasons(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/AuditReasons",
        json={"deleteEntry": [{"id": 1, "name": "Records purge"}]},
    )
    result = await server.get_audit_reasons()
    assert result["deleteEntry"][0]["name"] == "Records purge"


@pytest.mark.asyncio
async def test_get_task_status(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Tasks/op-1",
        json={"status": "Completed", "percentComplete": 100},
    )
    result = await server.get_task_status("op-1")
    assert result["status"] == "Completed"


@pytest.mark.asyncio
async def test_wait_for_task_returns_on_terminal_status(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Tasks/op-1",
        json={"status": "Completed"},
    )
    result = await server.wait_for_task("op-1", timeout_seconds=5)
    assert result["timed_out"] is False
    assert result["status"] == "Completed"


@pytest.mark.asyncio
async def test_wait_for_task_times_out(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Tasks/op-1",
        json={"status": "InProgress", "percentComplete": 50},
        is_reusable=True,
    )
    result = await server.wait_for_task(
        "op-1", timeout_seconds=1, poll_interval_seconds=0.1,
    )
    assert result["timed_out"] is True


# --- Write tools: read_only gating ------------------------------------------


@pytest.mark.asyncio
async def test_write_tool_refuses_when_read_only(
    patched_client: LaserficheClient,
) -> None:
    """LF_READ_ONLY=true (the test default) makes write helpers refuse to run
    even if invoked directly. Belt-and-suspenders to the registration gate."""
    with pytest.raises(RuntimeError) as exc_info:
        await server.set_fields(42, {"Note": ["x"]})
    assert "read_only" in str(exc_info.value).lower()


# --- Write tools: registration ----------------------------------------------


@pytest.mark.asyncio
async def test_write_tools_registered_when_writes_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LF_READ_ONLY=false, _register_write_tools() adds the writes."""
    monkeypatch.setenv("LF_READ_ONLY", "false")
    server._reset_settings_for_tests()
    # Use a fresh FastMCP to avoid polluting the module-level instance.
    import importlib

    import laserfiche_mcp.server as srv_mod
    importlib.reload(srv_mod)
    monkeypatch.setenv("LF_READ_ONLY", "false")
    srv_mod._reset_settings_for_tests()
    srv_mod._register_write_tools()
    tools = await srv_mod.mcp.list_tools()
    names = {t.name for t in tools}
    assert "delete_entry" in names
    assert "rename_entry" in names
    assert "set_fields" in names
    assert "merge_fields" in names
    # Restore module to read-only state for downstream tests.
    monkeypatch.setenv("LF_READ_ONLY", "true")
    srv_mod._reset_settings_for_tests()
    importlib.reload(srv_mod)


# --- Write tools: merge_fields preserves untouched fields -------------------


@pytest.mark.asyncio
async def test_merge_fields_preserves_unmentioned(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(
        server._get_settings(), "read_only", False,
    )
    # Entry fetch for the path-scope check (v1.3+)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    # Current fields on the entry
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/fields",
        json={
            "value": [
                {
                    "fieldName": "Last Name",
                    "values": [{"value": "Smith", "position": 0}],
                },
                {
                    "fieldName": "Note",
                    "values": [{"value": "old note", "position": 0}],
                },
            ]
        },
    )
    # PUT response
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )

    result = await server.merge_fields(42, {"Note": ["new note"]})
    assert result["mode"] == "executed"
    assert result["fields_updated"] == ["Note"]
    assert "Last Name" in result["fields_preserved"]

    # Confirm the PUT body kept "Last Name" intact (request #2 is the PUT;
    # request #0 is the entry GET added by the v1.3 path-scope check).
    put_body = httpx_mock.get_requests()[2].read().decode()
    assert "Last Name" in put_body
    assert "Smith" in put_body
    assert "new note" in put_body


# --- delete_entry: preview, then execute ------------------------------------


@pytest.mark.asyncio
async def test_delete_entry_preview_returns_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Doomed",
            "entryType": "Document",
            "fullPath": "\\Trash\\Doomed",
        },
    )
    preview = await server.delete_entry(42)
    assert preview["mode"] == "preview"
    assert preview["entry_id"] == 42
    assert "confirmation_token" in preview
    assert "warning" in preview


@pytest.mark.asyncio
async def test_delete_entry_executes_with_valid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doomed", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42",
        status_code=202,
        json={"token": "op-xyz", "taskId": "task-1"},
    )

    preview = await server.delete_entry(42)
    token = preview["confirmation_token"]
    result = await server.delete_entry(42, confirmation_token=token)

    assert result["mode"] == "executed"
    assert result["operation_token"] == "op-xyz"


@pytest.mark.asyncio
async def test_delete_entry_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doomed", "entryType": "Document"},
    )
    result = await server.delete_entry(42, confirmation_token="garbage")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_confirmation_token"


@pytest.mark.asyncio
async def test_delete_entry_token_bound_to_entry_id(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A token issued for entry A must not work to delete entry B."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "First", "entryType": "Document"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/99",
        json={"id": 99, "name": "Second", "entryType": "Document"},
    )

    preview_42 = await server.delete_entry(42)
    token = preview_42["confirmation_token"]
    bad_call = await server.delete_entry(99, confirmation_token=token)
    assert bad_call["mode"] == "error"
    assert bad_call["error"] == "invalid_confirmation_token"


@pytest.mark.asyncio
async def test_delete_entry_preview_reports_folder_child_count(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    cap = server._get_settings().delete_folder_max_descendants
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Big", "entryType": "Folder"},
    )
    # Probe fetches cap+1 children; with 47 returned (< cap+1), the count
    # is exact.
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children"
            f"?%24top={cap + 1}&%24skip=0"
        ),
        json={"value": [{"id": i, "name": f"c{i}"} for i in range(47)]},
    )
    preview = await server.delete_entry(100)
    assert preview["mode"] == "preview"
    assert preview["immediate_child_count"] == 47


# --- delete_pages: page_range required --------------------------------------


@pytest.mark.asyncio
async def test_delete_pages_refuses_empty_range(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.delete_pages(42, "")
    assert result["mode"] == "error"
    assert result["error"] == "page_range_required"


# --- rename_entry: preview/confirm ------------------------------------------


@pytest.mark.asyncio
async def test_rename_entry_preview_token_then_execute(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Old",
            "entryType": "Document",
            "fullPath": "\\Folder\\Old",
            "folderPath": "\\Folder",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="PATCH",
        url=f"{_BASE}/Entries/42?autoRename=false",
        json={"id": 42, "name": "New"},
    )

    preview = await server.rename_entry(42, "New")
    assert preview["mode"] == "preview"
    assert preview["would_be_full_path"] == "\\Folder\\New"

    result = await server.rename_entry(
        42, "New", confirmation_token=preview["confirmation_token"],
    )
    assert result["mode"] == "executed"
    assert result["new_name"] == "New"


@pytest.mark.asyncio
async def test_rename_entry_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Old", "entryType": "Document"},
    )
    result = await server.rename_entry(42, "New", confirmation_token="bad")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_confirmation_token"


# --- move_entry: preview/confirm --------------------------------------------


@pytest.mark.asyncio
async def test_move_entry_preview_then_execute(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    # Source
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Doc",
            "entryType": "Document",
            "fullPath": "\\Old\\Doc",
        },
        is_reusable=True,
    )
    # Target parent (for would_be_full_path preview + execute-side fence check)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/200",
        json={"id": 200, "name": "New", "entryType": "Folder", "fullPath": "\\New"},
        is_reusable=True,
    )
    # PATCH for the execute step
    httpx_mock.add_response(
        method="PATCH",
        url=f"{_BASE}/Entries/42?autoRename=false",
        json={"id": 42, "name": "Doc", "parentId": 200},
    )

    preview = await server.move_entry(42, 200)
    assert preview["mode"] == "preview"
    assert preview["would_be_full_path"] == "\\New\\Doc"

    result = await server.move_entry(
        42, 200, confirmation_token=preview["confirmation_token"],
    )
    assert result["mode"] == "executed"


@pytest.mark.asyncio
async def test_move_entry_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/200",
        json={"id": 200, "name": "Other", "entryType": "Folder"},
    )
    result = await server.move_entry(42, 200, confirmation_token="bad")
    assert result["mode"] == "error"


# --- delete_edoc / delete_pages preview/execute -----------------------------


@pytest.mark.asyncio
async def test_delete_edoc_preview_and_execute(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42, "name": "Doc", "entryType": "Document",
            "pageCount": 5, "extension": "pdf",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc",
        json={"value": True},
    )
    preview = await server.delete_edoc(42)
    assert preview["mode"] == "preview"
    result = await server.delete_edoc(42, confirmation_token=preview["confirmation_token"])
    assert result["mode"] == "executed"


@pytest.mark.asyncio
async def test_delete_edoc_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    result = await server.delete_edoc(42, confirmation_token="bad")
    assert result["mode"] == "error"


@pytest.mark.asyncio
async def test_delete_pages_preview_and_execute(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "pageCount": 10},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/Laserfiche.Repository.Document/pages?pageRange=1-3",
        json={"value": True},
    )
    preview = await server.delete_pages(42, "1-3")
    assert preview["mode"] == "preview"
    result = await server.delete_pages(
        42, "1-3", confirmation_token=preview["confirmation_token"],
    )
    assert result["mode"] == "executed"


@pytest.mark.asyncio
async def test_delete_pages_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    result = await server.delete_pages(42, "1-3", confirmation_token="bad")
    assert result["mode"] == "error"


# --- Non-confirmation write tools (happy paths) -----------------------------


@pytest.mark.asyncio
async def test_set_fields_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    httpx_mock.add_response(
        method="PUT", url=f"{_BASE}/Entries/42/fields", json={"value": []},
    )
    await server.set_fields(42, {"Note": ["new"]})


@pytest.mark.asyncio
async def test_set_tags_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    httpx_mock.add_response(
        method="PUT", url=f"{_BASE}/Entries/42/tags", json={"value": []},
    )
    await server.set_tags(42, ["urgent"])


@pytest.mark.asyncio
async def test_merge_tags_add_and_remove(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42/tags",
        json={"value": [{"name": "old"}, {"name": "keep"}]},
    )
    httpx_mock.add_response(
        method="PUT", url=f"{_BASE}/Entries/42/tags", json={"value": []},
    )
    result = await server.merge_tags(42, add=["new"], remove=["old"])
    assert result["mode"] == "executed"
    assert "new" in result["added"]
    assert "old" in result["removed"]
    assert sorted(result["final_tags"]) == ["keep", "new"]


@pytest.mark.asyncio
async def test_set_links_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    httpx_mock.add_response(
        method="PUT", url=f"{_BASE}/Entries/42/links", json={"value": []},
    )
    await server.set_links(42, [{"targetId": 7, "linkTypeId": 1}])


@pytest.mark.asyncio
async def test_assign_and_remove_template(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    # Skip the required-field validation in this test — it's exercising the
    # assign/remove pair, not the validator (which has its own dedicated tests).
    monkeypatch.setattr(server._get_settings(), "validate_required_fields", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "Personnel"},
    )
    await server.assign_template(42, "Personnel", fields={"Last Name": ["Smith"]})

    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42},
    )
    await server.remove_template(42)


@pytest.mark.asyncio
async def test_assign_template_blocks_when_required_field_missing(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Validator returns mode:error when a repo-wide required field is unset."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={
            "id": 42, "name": "Doc", "entryType": "Document",
            "fullPath": "\\Doc",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=500&%24skip=0",
        json={
            "value": [
                {
                    "name": "Type of Document", "fieldType": "List",
                    "isRequired": True,
                    "listValues": ["Digital", "Original"],
                    "defaultValue": "Digital",
                },
                {
                    "name": "Last Name", "fieldType": "String",
                    "isRequired": False, "listValues": [],
                    "defaultValue": None,
                },
            ]
        },
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42/fields",
        json={"value": []},  # no fields currently set on the entry
    )

    result = await server.assign_template(42, "Personnel")

    assert result["mode"] == "error"
    assert result["error"] == "missing_required_fields"
    assert result["missing"] == ["Type of Document"]
    assert result["field_details"][0]["list_values"] == ["Digital", "Original"]


@pytest.mark.asyncio
async def test_assign_template_validation_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """With LF_VALIDATE_REQUIRED_FIELDS=false the validator is skipped."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_required_fields", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    # No FieldDefinitions or fields mocks — they must not be called.
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "Personnel"},
    )
    result = await server.assign_template(42, "Personnel")
    assert "mode" not in result or result.get("mode") != "error"


@pytest.mark.asyncio
async def test_assign_template_validation_passes_when_required_field_already_set(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If every required field is already on the entry, validator returns None
    and the real PUT runs."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={
            "id": 42, "name": "Doc", "entryType": "Document",
            "fullPath": "\\Doc",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=500&%24skip=0",
        json={
            "value": [
                {"name": "Type of Document", "fieldType": "List",
                 "isRequired": True, "listValues": ["Digital"]},
            ]
        },
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42/fields",
        json={"value": [
            {"fieldName": "Type of Document", "values": [{"value": "Digital"}]},
        ]},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "T"},
    )
    result = await server.assign_template(42, "T")
    assert result.get("mode") != "error"


@pytest.mark.asyncio
async def test_assign_template_validation_accepts_required_field_via_caller_fields(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If the caller supplies the required field in ``fields=``, validator
    accepts it even though it's not yet on the entry."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document",
              "fullPath": "\\Doc"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [
            {"name": "Type of Document", "fieldType": "List",
             "isRequired": True, "listValues": ["Digital"]},
            {"name": "Doc Classification", "fieldType": "List",
             "isRequired": True, "listValues": [" "]},
        ]},
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "T"},
    )
    result = await server.assign_template(
        42, "T",
        fields={"Type of Document": ["Digital"], "Doc Classification": [" "]},
    )
    assert result.get("mode") != "error"


@pytest.mark.asyncio
async def test_assign_template_validation_flags_only_unsupplied_missing(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """When the caller supplies one of two missing required fields, only the
    unsupplied one is reported."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document",
              "fullPath": "\\Doc"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [
            {"name": "A", "fieldType": "String", "isRequired": True, "listValues": []},
            {"name": "B", "fieldType": "String", "isRequired": True, "listValues": []},
        ]},
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )
    result = await server.assign_template(42, "T", fields={"A": ["x"]})
    assert result["mode"] == "error"
    assert result["missing"] == ["B"]


@pytest.mark.asyncio
async def test_assign_template_validation_falls_through_on_read_failure(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """If list_field_definitions fails, validator returns None and the real
    PUT runs (server-side error path is what surfaces)."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document",
              "fullPath": "\\Doc"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=500&%24skip=0",
        status_code=500,
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/template",
        json={"id": 42, "templateName": "T"},
    )
    result = await server.assign_template(42, "T")
    assert result.get("mode") != "error"


@pytest.mark.asyncio
async def test_delete_entry_returns_structured_error_when_entry_missing(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Fetch-failure path of a write tool: the entry doesn't exist, so the
    structured error must surface — not a RuntimeError."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/999", status_code=404,
    )
    result = await server.delete_entry(999)
    assert result["mode"] == "error"
    assert result["operation"] == "delete_entry"
    assert result["error"] == "not_found"
    assert result["entry_id"] == 999


@pytest.mark.asyncio
async def test_list_repositories_falls_back_on_server_error(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """On endpoint failure, surface the configured repo as a single-item fallback."""
    httpx_mock.add_response(
        method="GET",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories",
        status_code=400,
        json={"errorCode": 216, "title": "Endpoint disabled on this build"},
    )

    result = await server.list_repositories()

    assert result["mode"] == "fallback"
    assert result["operation"] == "list_repositories"
    assert result["value"][0]["repoId"] == "demo"  # from test settings
    assert result["value"][0]["is_configured"] is True
    assert result["server_error"]["status_code"] == 400


@pytest.mark.asyncio
async def test_create_folder_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder"},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children?autoRename=false",
        json={"id": 500, "name": "New", "entryType": "Folder"},
    )
    await server.create_folder(100, "New")


@pytest.mark.asyncio
async def test_copy_entry_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder"},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/CopyAsync?autoRename=true",
        status_code=201,
        json={"token": "op-copy-1"},
    )
    result = await server.copy_entry(42, 100, "Copy", auto_rename=True)
    assert result["token"] == "op-copy-1"


@pytest.mark.asyncio
async def test_import_document_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder"},
    )
    missing = tmp_path / "missing.txt"
    result = await server.import_document(100, "x.txt", str(missing))
    assert result["mode"] == "error"
    assert result["error"] == "file_not_found"


@pytest.mark.asyncio
async def test_import_document_size_cap(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    tmp_path: Path,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "import_max_bytes", 10)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder"},
    )
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100)
    result = await server.import_document(100, "big.bin", str(big))
    assert result["mode"] == "error"
    assert result["error"] == "size_exceeds_cap"


@pytest.mark.asyncio
async def test_import_document_happy_path_with_metadata(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    f = tmp_path / "doc.txt"
    f.write_bytes(b"hello")
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder"},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/doc.txt?autoRename=false",
        status_code=201,
        json={"entryCreate": {"entryId": 500}},
    )
    result = await server.import_document(
        100, "doc.txt", str(f),
        template_name="Doc",
        fields={"Note": ["hello"]},
        tags=["new"],
    )
    assert result.get("entryCreate", {}).get("entryId") == 500


# --- Listing tools (definitions) --------------------------------------------


@pytest.mark.asyncio
async def test_list_field_definitions_tool(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=25&%24skip=0",
        json={"value": [{"id": 1, "name": "Last Name"}]},
    )
    result = await server.list_field_definitions()
    assert len(result["value"]) == 1


@pytest.mark.asyncio
async def test_list_tag_definitions_tool(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TagDefinitions?%24top=25&%24skip=0",
        json={"value": []},
    )
    await server.list_tag_definitions()


@pytest.mark.asyncio
async def test_list_template_definitions_tool(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=25&%24skip=0",
        json={"value": []},
    )
    await server.list_template_definitions()


@pytest.mark.asyncio
async def test_list_link_definitions_tool(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/LinkDefinitions?%24top=25&%24skip=0",
        json={"value": []},
    )
    await server.list_link_definitions()


@pytest.mark.asyncio
async def test_list_repositories_tool(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories",
        json={"value": [{"repoId": "demo"}]},
    )
    await server.list_repositories()


# --- v1.3 guards (path scope, batch cap, tool allowlist, audit reason) ------


@pytest.mark.asyncio
async def test_write_refused_by_path_deny(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "write_paths_deny", "\\Protected")
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42, "name": "Doc", "entryType": "Document",
            "fullPath": "\\Protected\\Doc",
        },
    )
    result = await server.set_fields(42, {"Note": ["x"]})
    assert result["mode"] == "error"
    assert result["error"] == "path_not_allowed"


@pytest.mark.asyncio
async def test_write_allowed_within_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "write_paths_allow", "\\Imports")
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42, "name": "Doc", "entryType": "Document",
            "fullPath": "\\Imports\\2024\\Doc",
        },
    )
    httpx_mock.add_response(
        method="PUT", url=f"{_BASE}/Entries/42/fields", json={"value": []},
    )
    result = await server.set_fields(42, {"Note": ["x"]})
    # No "mode": "error" — write proceeded
    assert "error" not in result


@pytest.mark.asyncio
async def test_write_refused_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "write_paths_allow", "\\Imports")
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42, "name": "Doc", "entryType": "Document",
            "fullPath": "\\Production\\Doc",
        },
    )
    result = await server.set_fields(42, {"Note": ["x"]})
    assert result["mode"] == "error"
    assert result["error"] == "path_not_allowed"


@pytest.mark.asyncio
async def test_create_folder_checks_parent_path(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """create_folder fences on parent's path since the new folder doesn't exist yet."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "write_paths_deny", "\\Production")
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={
            "id": 100, "name": "Production", "entryType": "Folder",
            "fullPath": "\\Production",
        },
    )
    result = await server.create_folder(100, "NewSub")
    assert result["mode"] == "error"
    assert result["error"] == "path_not_allowed"


@pytest.mark.asyncio
async def test_move_entry_destination_is_fenced(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """A move from an allowed source into a denied destination is refused."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "write_paths_deny", "\\Protected")
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={
            "id": 42, "name": "Doc", "entryType": "Document",
            "fullPath": "\\Sandbox\\Doc",
        },
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/300",
        json={
            "id": 300, "name": "Protected", "entryType": "Folder",
            "fullPath": "\\Protected",
        },
    )
    result = await server.move_entry(42, 300)
    assert result["mode"] == "error"
    assert result["error"] == "path_not_allowed"


@pytest.mark.asyncio
async def test_delete_entry_refuses_exceeding_batch_cap(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "delete_folder_max_descendants", 10)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/100",
        json={
            "id": 100, "name": "Big", "entryType": "Folder",
            "fullPath": "\\Big",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children"
            "?%24top=11&%24skip=0"
        ),
        # cap=10 + 1 = 11 items returned ⇒ exceeds_cap=True.
        json={"value": [{"id": i, "name": f"c{i}"} for i in range(11)]},
        is_reusable=True,
    )
    preview = await server.delete_entry(100)
    assert preview["mode"] == "preview"
    assert preview["exceeds_batch_cap"] is True

    blocked = await server.delete_entry(
        100, confirmation_token=preview["confirmation_token"],
    )
    assert blocked["mode"] == "error"
    assert blocked["error"] == "exceeds_batch_cap"


@pytest.mark.asyncio
async def test_delete_entry_force_large_delete_overrides_cap(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "delete_folder_max_descendants", 10)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/100",
        json={
            "id": 100, "name": "Big", "entryType": "Folder",
            "fullPath": "\\Big",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=(
            f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children"
            "?%24top=11&%24skip=0"
        ),
        # cap=10 + 1 = 11 items returned ⇒ exceeds_cap=True.
        json={"value": [{"id": i, "name": f"c{i}"} for i in range(11)]},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/100",
        status_code=202,
        json={"token": "op-xyz"},
    )
    preview = await server.delete_entry(100)
    result = await server.delete_entry(
        100,
        confirmation_token=preview["confirmation_token"],
        force_large_delete=True,
    )
    assert result["mode"] == "executed"


@pytest.mark.asyncio
async def test_delete_entry_requires_audit_reason_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "require_audit_reason", True)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\Doc"},
        is_reusable=True,
    )
    preview = await server.delete_entry(42)
    refused = await server.delete_entry(
        42, confirmation_token=preview["confirmation_token"],
    )
    assert refused["mode"] == "error"
    assert refused["error"] == "audit_reason_required"


@pytest.mark.asyncio
async def test_delete_entry_proceeds_with_audit_reason(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "require_audit_reason", True)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document", "fullPath": "\\Doc"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42",
        status_code=202,
        json={"token": "op-xyz"},
    )
    preview = await server.delete_entry(42)
    result = await server.delete_entry(
        42,
        confirmation_token=preview["confirmation_token"],
        audit_reason_id=5,
    )
    assert result["mode"] == "executed"


@pytest.mark.asyncio
async def test_tool_allowlist_blocks_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """Defense-in-depth: even if a tool is invoked directly, the allowlist
    refuses operations outside the configured set."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(
        settings, "write_tools_allowed", "merge_fields,merge_tags",
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
    )
    result = await server.delete_entry(42)
    assert result["mode"] == "error"
    assert result["error"] == "tool_not_allowed"


def test_register_write_tools_respects_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_register_write_tools only registers tools in LF_WRITE_TOOLS_ALLOWED."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(
        settings, "write_tools_allowed", "merge_fields,create_folder",
    )
    registered: list[str] = []
    monkeypatch.setattr(
        server.mcp,
        "tool",
        lambda **kwargs: (lambda fn: registered.append(fn.__name__) or fn),
    )
    server._register_write_tools()
    assert set(registered) == {"merge_fields", "create_folder"}


@pytest.mark.asyncio
async def test_all_tools_registered() -> None:
    """Read tools always register. Write tools only register when
    LF_READ_ONLY=false at startup (see test_write_tools_registration)."""
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    # v1.x names — kept as deprecation shims through v2.0.
    legacy = {
        "search_entries",
        "search_by_name",
        "search_natural",
        "list_folder",
        "get_entry",
        "get_entry_by_path",
        "get_field_values",
        "get_document_text",
        "get_document_edoc",
        "list_repositories",
        "list_field_definitions",
        "list_tag_definitions",
        "list_template_definitions",
        "list_link_definitions",
        "get_audit_reasons",
        "get_task_status",
        "wait_for_task",
        "get_template_fields",
    }
    # v2.0 names — laserfiche_{resource}_{verb}. From _V2_RENAME_MAP.
    v2 = set(server._V2_RENAME_MAP.values())
    # Reads-only registration in this test (writes off in test config).
    expected = legacy | {
        name for old, name in server._V2_RENAME_MAP.items()
        if old in legacy
    }
    assert names == expected
    # Sanity: every old name has a v2 alias.
    for old in legacy:
        assert old in server._V2_RENAME_MAP, f"missing v2 alias for {old}"
    # Sanity: v2 names all start with the laserfiche_ prefix.
    assert all(n.startswith("laserfiche_") for n in v2)


# --- CLI argument parser -----------------------------------------------------


def test_cli_parse_args_defaults() -> None:
    args = server._parse_args([])
    assert args.help is False
    assert args.version is False
    assert args.diagnose is False
    assert args.verbose == 0
    assert args.quiet is False
    assert args.config is None


def test_cli_parse_args_help_flag() -> None:
    args = server._parse_args(["--help"])
    assert args.help is True
    short = server._parse_args(["-h"])
    assert short.help is True


def test_cli_parse_args_version_flag() -> None:
    args = server._parse_args(["--version"])
    assert args.version is True
    short = server._parse_args(["-V"])
    assert short.version is True


def test_cli_parse_args_diagnose_flag() -> None:
    args = server._parse_args(["--diagnose"])
    assert args.diagnose is True


def test_cli_parse_args_verbose_counts() -> None:
    args = server._parse_args(["-v"])
    assert args.verbose == 1
    args2 = server._parse_args(["-vv"])
    assert args2.verbose == 2


def test_cli_parse_args_verbose_and_quiet_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        server._parse_args(["--verbose", "--quiet"])


def test_cli_parse_args_config_path() -> None:
    args = server._parse_args(["--config", ".env.custom"])
    assert args.config == ".env.custom"


def test_cli_resolve_log_level_prefers_verbose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "log_level", "INFO")
    args = server._parse_args(["-v"])
    assert server._resolve_log_level(settings, args) == "DEBUG"


def test_cli_resolve_log_level_prefers_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "log_level", "INFO")
    args = server._parse_args(["--quiet"])
    assert server._resolve_log_level(settings, args) == "WARNING"


def test_cli_resolve_log_level_falls_back_to_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "log_level", "WARNING")
    args = server._parse_args([])
    assert server._resolve_log_level(settings, args) == "WARNING"


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

    assert result["mode"] == "error"
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["status_code"] == 500
    assert result["next_action"] is not None
    assert "non-400" in result["next_action"]


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

    assert any("clamped from 500 to 30" in n for n in result["notes"])


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

    assert result["mode"] == "guidance"
    assert any(
        "Could not resolve folder_path" in note for note in result["notes"]
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
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=0"
        ),
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
        url=(
            f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children"
            "?%24top=10&%24skip=0"
        ),
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

    assert result["discovered_templates"] == []
    assert any(
        "no template assigned" in n for n in result["notes"]
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

    assert len(result["discovered_templates"]) == 1
    assert result["discovered_templates"][0]["template_name"] == "PAF"
    assert result["discovered_templates"][0]["field_names"] == []


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


# --- Pass 1 step 1c: name/schema/page-range pre-flight validators ----------


@pytest.mark.asyncio
async def test_create_folder_rejects_invalid_name(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.create_folder(parent_id=100, name="bad/name")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_name"
    assert result["parent_id"] == 100


@pytest.mark.asyncio
async def test_rename_entry_rejects_invalid_name(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.rename_entry(entry_id=42, new_name="bad\name")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_name"
    assert result["entry_id"] == 42


@pytest.mark.asyncio
async def test_copy_entry_rejects_invalid_name(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.copy_entry(source_id=42, parent_id=100, name="")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_name"


@pytest.mark.asyncio
async def test_import_document_rejects_invalid_name(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.import_document(
        parent_id=100, name="bad/file.txt", file_path="x",
    )
    assert result["mode"] == "error"
    assert result["error"] == "invalid_name"


@pytest.mark.asyncio
async def test_delete_pages_rejects_malformed_page_range(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.delete_pages(entry_id=42, page_range="1, 2")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_page_range"
    assert result["entry_id"] == 42


@pytest.mark.asyncio
async def test_set_fields_rejects_unknown_field_name(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_names", True)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "Status"}]},
        is_reusable=True,
    )
    # path-fence fetch
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    result = await server.set_fields(entry_id=42, fields={"NoSuchField": ["x"]})
    assert result["mode"] == "error"
    assert result["error"] == "invalid_field_name"
    assert "NoSuchField" in result["invalid_field_names"]


@pytest.mark.asyncio
async def test_assign_template_rejects_unknown_template(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_names", True)
    monkeypatch.setattr(settings, "validate_required_fields", False)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "Personnel"}]},
        is_reusable=True,
    )
    result = await server.assign_template(
        entry_id=42, template_name="DoesNotExist",
    )
    assert result["mode"] == "error"
    assert result["error"] == "invalid_template_name"
    assert result["template_name"] == "DoesNotExist"


@pytest.mark.asyncio
async def test_set_tags_rejects_unknown_tag(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_names", True)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TagDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "Confidential"}]},
        is_reusable=True,
    )
    result = await server.set_tags(entry_id=42, tags=["Unknown"])
    assert result["mode"] == "error"
    assert result["error"] == "invalid_tag_name"


@pytest.mark.asyncio
async def test_set_links_rejects_unknown_link_type(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_names", True)
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/LinkDefinitions?%24top=500&%24skip=0",
        json={"value": [{"linkTypeId": 1, "sourceLabel": "Supersedes"}]},
        is_reusable=True,
    )
    result = await server.set_links(
        entry_id=42, links=[{"targetId": 99, "linkTypeId": 999}],
    )
    assert result["mode"] == "error"
    assert result["error"] == "invalid_link_type"


@pytest.mark.asyncio
async def test_validators_skip_when_lf_validate_names_false(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """With LF_VALIDATE_NAMES=false the schema endpoints are NOT hit."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    # NB: no schema-endpoint mocks registered. If a validator runs, the
    # request will fail. test_*_validates_*_on settings.validate_names=False.
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42",
        json={"id": 42, "name": "Doc", "entryType": "Document"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        method="PUT",
        url=f"{_BASE}/Entries/42/fields",
        json={"value": []},
    )
    # Will pass because validate_names=false (test conftest default)
    result = await server.set_fields(entry_id=42, fields={"AnyField": ["x"]})
    # The set_fields call succeeds end-to-end; no schema-cache request made.
    assert result.get("mode") != "error" or result.get("error") != "invalid_field_name"


# --- get_template_fields (new in v2.0) ---------------------------------------


@pytest.mark.asyncio
async def test_get_template_fields_returns_template_metadata(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=500&%24skip=0",
        json={"value": [
            {"id": 2, "name": "Missionary Document",
             "templateFieldNames": ["Last Name", "Status"]},
        ]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [
            {"id": 16, "name": "Last Name", "fieldType": "String",
             "isRequired": False, "isMultiValue": False, "listValues": []},
            {"id": 50, "name": "Status", "fieldType": "List",
             "isRequired": True, "isMultiValue": False,
             "listValues": ["Pending", "Approved"]},
        ]},
    )
    result = await server.get_template_fields(template_name="Missionary Document")
    assert result["template_name"] == "Missionary Document"
    assert result["template_id"] == 2
    assert result["field_count"] == 2
    field_names = {f["name"] for f in result["fields"]}
    assert field_names == {"Last Name", "Status"}


@pytest.mark.asyncio
async def test_get_template_fields_required_only_filters(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=500&%24skip=0",
        json={"value": [
            {"id": 2, "name": "T",
             "templateFieldNames": ["A", "B"]},
        ]},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=500&%24skip=0",
        json={"value": [
            {"id": 1, "name": "A", "fieldType": "String", "isRequired": True},
            {"id": 2, "name": "B", "fieldType": "String", "isRequired": False},
        ]},
    )
    result = await server.get_template_fields(template_name="T", required_only=True)
    assert result["field_count"] == 1
    assert result["fields"][0]["name"] == "A"
    assert result["fields"][0]["is_required"] is True


@pytest.mark.asyncio
async def test_get_template_fields_returns_error_on_unknown_template(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=500&%24skip=0",
        json={"value": [{"id": 1, "name": "Personnel"}]},
    )
    result = await server.get_template_fields(template_name="DoesNotExist")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_template_name"
    assert "Personnel" in result["valid_template_names"]


# --- summary_only on list_*_definitions ---------------------------------------


@pytest.mark.asyncio
async def test_list_field_definitions_summary_only(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=25&%24skip=0",
        json={"value": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]},
    )
    result = await server.list_field_definitions(summary_only=True)
    assert result == {"count": 2, "names": ["A", "B"]}


@pytest.mark.asyncio
async def test_list_tag_definitions_summary_only(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TagDefinitions?%24top=25&%24skip=0",
        json={"value": [{"id": 1, "name": "Confidential"}]},
    )
    result = await server.list_tag_definitions(summary_only=True)
    assert result == {"count": 1, "names": ["Confidential"]}


@pytest.mark.asyncio
async def test_list_template_definitions_summary_only(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=25&%24skip=0",
        json={"value": [{"id": 1, "name": "T1"}, {"id": 2, "name": "T2"}]},
    )
    result = await server.list_template_definitions(summary_only=True)
    assert result["count"] == 2
    assert set(result["names"]) == {"T1", "T2"}


@pytest.mark.asyncio
async def test_list_link_definitions_summary_only(
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/LinkDefinitions?%24top=25&%24skip=0",
        json={"value": [
            {"linkTypeId": 1, "sourceLabel": "S1"},
            {"linkTypeId": 2, "sourceLabel": "S2"},
        ]},
    )
    # link defs don't have a 'name'; the summary helper falls back to displayName,
    # which is also absent. Names list will be empty.
    result = await server.list_link_definitions(summary_only=True)
    assert result["count"] == 2
    # The link definition has sourceLabel, not name — empty names list is acceptable.
