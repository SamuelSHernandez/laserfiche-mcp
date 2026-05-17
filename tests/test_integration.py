"""Opt-in integration tests that talk to a real Laserfiche Repository API.

These are **skipped by default** so the standard ``pytest`` run stays
hermetic. To opt in, set ``LF_INTEGRATION_TEST=1`` in the shell where you
invoke pytest. You also need a populated ``.env`` (or ``LF_*`` env vars)
pointing at a reachable repository — same configuration the server itself
uses at runtime.

Optional parameters (used as defaults when ``LF_INTEGRATION_TEST=1`` is set):

  LF_INTEGRATION_FOLDER_PATH    Folder to sample in Mode A guidance test.
                                Falls back to "\\" (repository root).
  LF_INTEGRATION_PDF_ENTRY_ID   Entry ID of a PDF document used by the edoc
                                tests. If unset, those tests are skipped
                                rather than failing on a missing entry.
  LF_INTEGRATION_SAFE_QUERY     A Laserfiche query that is expected to
                                return results on the target repo (e.g.
                                ``{LF:Name="*"}``). Defaults to that.

These tests exist for two reasons:
1. To prove the new ``search_natural`` Mode B repair path actually executes
   against the server class the feature was built for, not just against
   mocked HTTP.
2. To prove ``get_document_edoc(mode="text")`` extracts text from a real
   v1-server PDF, the canonical bug repro target from the spec.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from laserfiche_mcp import server
from laserfiche_mcp.auth import build_auth_strategy
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("LF_INTEGRATION_TEST") != "1",
        reason=(
            "Set LF_INTEGRATION_TEST=1 to opt in. Real-server tests require "
            "LF_REPO_API_URL, LF_REPOSITORY_ID, LF_USERNAME, LF_PASSWORD to "
            "point at a reachable Laserfiche repository."
        ),
    ),
]


@pytest.fixture
async def real_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[LaserficheClient]:
    """Build a real LaserficheClient from the host's LF_* env vars.

    Patches ``server._client()`` so the tool entrypoints can be awaited
    directly, same as the mocked tests.
    """
    # Reset cached settings so Settings() reads the real environment, not
    # whatever a previous test fixture cached.
    server._reset_settings_for_tests()
    settings = Settings()  # type: ignore[call-arg]
    auth = build_auth_strategy(settings)
    async with LaserficheClient(settings, auth) as client:
        monkeypatch.setattr(server, "_client", lambda: client)
        yield client


# --- search_natural ---------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_search_natural_mode_a_against_real_folder(
    real_client: LaserficheClient,
) -> None:
    """Mode A guidance must return without raising against a real repo."""
    folder_path = os.environ.get("LF_INTEGRATION_FOLDER_PATH", "\\")

    result = await server.search_natural(
        question="any record",
        folder_path=folder_path,
    )

    assert result.mode == "guidance"
    assert result.grammar is not None
    assert result.candidate_queries, "Mode A should always return at least one candidate"
    assert result.follow_up is not None


@pytest.mark.asyncio
async def test_integration_search_natural_mode_b_surfaces_structured_outcome(
    real_client: LaserficheClient,
) -> None:
    """Mode B should produce either results or a structured error — never a raw exception.

    This is the canonical contract the spec was built to deliver: when the
    server rejects a query, the host LLM gets attempts/next_action, not a
    Python traceback.
    """
    query = os.environ.get("LF_INTEGRATION_SAFE_QUERY", '{LF:Name="*"}')

    result = await server.search_natural(
        question="probe",
        lf_query=query,
        max_results=5,
    )

    assert result.mode in {"results", "error"}
    if result.mode == "error":
        # If the server rejects every attempt, the structured error must
        # carry diagnostics — not just an empty shape.
        assert result.attempts, "error mode must record at least one attempt"
        assert result.next_action, "error mode must include a next_action hint"


# --- get_document_edoc ------------------------------------------------------


def _pdf_entry_id() -> int | None:
    raw = os.environ.get("LF_INTEGRATION_PDF_ENTRY_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@pytest.mark.asyncio
async def test_integration_edoc_info_against_known_pdf(
    real_client: LaserficheClient,
) -> None:
    entry_id = _pdf_entry_id()
    if entry_id is None:
        pytest.skip("Set LF_INTEGRATION_PDF_ENTRY_ID to a known PDF entry to run this test.")

    result = await server.get_document_edoc(entry_id=entry_id, mode="info")

    assert result["mode"] == "info"
    assert result["byte_size"] > 0
    assert result["content_type"] is not None


@pytest.mark.asyncio
async def test_integration_edoc_text_against_known_pdf(
    real_client: LaserficheClient,
) -> None:
    """The whole reason mode='text' exists: read a PDF from a v1 server.

    The fixture this targets is the canonical regression test from the
    spec — once this passes, Part 2 is end-to-end validated against a
    real server. If the entry isn't a PDF, the test asserts a structured
    error (which is also a valid outcome for the workflow).
    """
    entry_id = _pdf_entry_id()
    if entry_id is None:
        pytest.skip("Set LF_INTEGRATION_PDF_ENTRY_ID to a known PDF entry to run this test.")

    result = await server.get_document_edoc(entry_id=entry_id, mode="text")

    assert result["mode"] == "text"
    if "error" in result:
        # Permitted outcomes against a real entry: encrypted / non-PDF /
        # malformed. We require the error to be structured (not raised).
        assert result["error"] in {
            "pdf_encrypted",
            "pdf_open_failed",
            "pdf_extraction_failed",
            "unsupported_content_type",
            "size_exceeds_cap",
        }
        assert result.get("message")
    else:
        assert isinstance(result["text"], str)
        assert result["pages_total"] >= 1


# --- Structured error contract through FastMCP --------------------------------


@pytest.mark.asyncio
async def test_integration_get_entry_missing_returns_structured_error(
    real_client: LaserficheClient,
) -> None:
    """The structured error contract must survive FastMCP's runtime validation.

    This is the regression for Bug 8 (v1.4.x): typed-return tools silently
    leaked pydantic detail when the underlying response was an error dict.
    After the refactor, the tool returns a dict on both paths, so the LLM
    sees the slug + reason cleanly.
    """
    result = await server.get_entry(entry_id=999_999_999)
    assert isinstance(result, dict)
    assert result.get("mode") == "error"
    assert result.get("error") in {"not_found", "auth_failed"}
    assert result.get("entry_id") == 999_999_999
    assert result.get("reason")


@pytest.mark.asyncio
async def test_integration_get_entry_by_path_missing_returns_structured_error(
    real_client: LaserficheClient,
) -> None:
    """Same as above, for the path-resolution variant."""
    result = await server.get_entry_by_path(full_path="\\__definitely-missing-path__")
    assert isinstance(result, dict)
    # Some v1 builds return id=0 sentinel instead of a 404; both are acceptable.
    if result.get("mode") == "error":
        assert result.get("error") in {"not_found", "auth_failed"}
    else:
        assert result.get("id") == 0


@pytest.mark.asyncio
async def test_integration_list_folder_missing_returns_structured_error(
    real_client: LaserficheClient,
) -> None:
    result = await server.list_folder(folder_id=999_999_999)
    assert isinstance(result, dict)
    assert result.get("mode") == "error"
    assert result.get("error") in {"not_found", "auth_failed"}
    assert result.get("folder_id") == 999_999_999


@pytest.mark.asyncio
async def test_integration_get_field_values_missing_returns_structured_error(
    real_client: LaserficheClient,
) -> None:
    result = await server.get_field_values(entry_id=999_999_999)
    assert isinstance(result, dict)
    assert result.get("mode") == "error"
    assert result.get("error") in {"not_found", "auth_failed"}
    assert result.get("entry_id") == 999_999_999


# --- list_repositories: list-shape normalization (Bug 7) ----------------------


@pytest.mark.asyncio
async def test_integration_list_repositories_returns_envelope_shape(
    real_client: LaserficheClient,
) -> None:
    """list_repositories must return ``{"value": [...]}`` regardless of whether
    this server's build sends an OData envelope or a bare list.

    Regression for Bug 7 (v1.4.x).
    """
    result = await server.list_repositories()
    # mode is only set on the fallback path; success path uses the raw envelope.
    if result.get("mode") == "fallback":
        # Endpoint disabled on this build; fallback gives us the configured repo.
        assert isinstance(result["value"], list)
        assert len(result["value"]) == 1
        assert result["value"][0].get("is_configured") is True
    else:
        assert isinstance(result.get("value"), list)
        assert all(isinstance(r, dict) for r in result["value"])


# --- Path-fence enforcement ----------------------------------------------------


def _writes_enabled() -> bool:
    """Tests that exercise writes require both LF_INTEGRATION_TEST=1 AND
    LF_READ_ONLY=false. Skip otherwise; we never write to an unprepared repo."""
    return os.environ.get("LF_READ_ONLY", "true").lower() == "false"


def _sandbox_parent_id() -> int | None:
    raw = os.environ.get("LF_INTEGRATION_SANDBOX_PARENT_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@pytest.mark.asyncio
async def test_integration_path_fence_blocks_disallowed_destination(
    real_client: LaserficheClient,
) -> None:
    """move_entry must refuse a destination outside LF_WRITE_PATHS_ALLOW.

    Set LF_INTEGRATION_SANDBOX_PARENT_ID to an entry inside your allow list
    (a doc or folder is fine); the test attempts to move it to the root (1),
    which is intentionally outside any reasonable sandbox. The MCP must
    return ``path_not_allowed`` BEFORE the API call.
    """
    if not _writes_enabled():
        pytest.skip("Writes disabled (LF_READ_ONLY!=false).")
    sandbox_id = _sandbox_parent_id()
    if sandbox_id is None:
        pytest.skip("Set LF_INTEGRATION_SANDBOX_PARENT_ID to an in-allowlist entry.")

    result = await server.move_entry(entry_id=sandbox_id, new_parent_id=1)
    assert isinstance(result, dict)
    assert result.get("error") == "path_not_allowed"


# --- assign_template required-field validator --------------------------------


@pytest.mark.asyncio
async def test_integration_assign_template_validator_blocks_when_required_field_missing(
    real_client: LaserficheClient,
) -> None:
    """assign_template should refuse with missing_required_fields BEFORE the
    PUT when a repo-wide required field isn't set on the target entry and
    isn't supplied in fields=.

    Requires:
      LF_INTEGRATION_SANDBOX_PARENT_ID — an entry the service account can
        attempt to template (the call is preflighted client-side, so the
        validator fires before the API mutation; this is non-destructive).
      LF_INTEGRATION_TEMPLATE_NAME — a template that targets the entry's
        type. Defaults to the first template returned by
        list_template_definitions if unset.
    """
    if not _writes_enabled():
        pytest.skip("Writes disabled (LF_READ_ONLY!=false).")
    sandbox_id = _sandbox_parent_id()
    if sandbox_id is None:
        pytest.skip("Set LF_INTEGRATION_SANDBOX_PARENT_ID to an in-allowlist entry.")

    # Look up the first available template (or use override).
    template_name = os.environ.get("LF_INTEGRATION_TEMPLATE_NAME")
    if template_name is None:
        templates = await server.list_template_definitions(max_results=1)
        value = templates.get("value") or []
        if not value:
            pytest.skip("No templates available on this repo to assign.")
        template_name = value[0]["name"]

    # Probe required-field set; if there are none, the validator can't trigger.
    defs = await server.list_field_definitions(max_results=500)
    required = [
        f for f in defs.get("value", []) if f.get("isRequired") and (f.get("name") or "").strip()
    ]
    if not required:
        pytest.skip("No repo-required fields configured; validator can't trigger.")

    result = await server.assign_template(
        entry_id=sandbox_id,
        template_name=template_name,
    )
    assert isinstance(result, dict)
    # Either the validator catches it (preferred), or the server does and we
    # surface the classified error. Both are acceptable contract outcomes.
    assert result.get("mode") == "error"
    assert result.get("error") in {"missing_required_fields", "required_field_missing"}
    if result.get("error") == "missing_required_fields":
        assert isinstance(result.get("missing"), list)
        assert len(result["missing"]) >= 1


# --- Folder-delete preview + child-count accuracy -----------------------------


@pytest.mark.asyncio
async def test_integration_delete_folder_preview_reports_accurate_child_count(
    real_client: LaserficheClient,
) -> None:
    """delete_entry preview must report a numerically-correct child count.

    Regression for the page-bound `$count` issue: v1.4 reworked the probe to
    fetch cap+1 items and use len(items) because the server's `$count` is
    page-bound on this build. Verifies the preview surfaces a real number
    (not None) when the folder is at or under the cap.

    Requires LF_INTEGRATION_SANDBOX_PARENT_ID to point at a folder.
    Non-destructive: only the preview is issued; no confirmation token sent.
    """
    if not _writes_enabled():
        pytest.skip("Writes disabled (LF_READ_ONLY!=false).")
    sandbox_id = _sandbox_parent_id()
    if sandbox_id is None:
        pytest.skip("Set LF_INTEGRATION_SANDBOX_PARENT_ID to a folder ID.")

    entry = await server.get_entry(entry_id=sandbox_id)
    if entry.get("entry_type") != "Folder":
        pytest.skip("LF_INTEGRATION_SANDBOX_PARENT_ID must be a folder for this test.")

    preview = await server.delete_entry(entry_id=sandbox_id)
    assert isinstance(preview, dict)
    if preview.get("mode") == "error":
        pytest.skip(f"Preview refused with {preview.get('error')!r}; can't measure.")

    assert preview["mode"] == "preview"
    assert preview["entry_type"] == "Folder"
    # Either the cap was exceeded (count=None, flag=True) or the count is an
    # actual integer matching what list_folder sees.
    if preview.get("exceeds_batch_cap"):
        assert preview.get("immediate_child_count") is None
    else:
        listing = await server.list_folder(folder_id=sandbox_id, max_results=200)
        assert preview["immediate_child_count"] == len(listing["entries"])
