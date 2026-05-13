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
        pytest.skip(
            "Set LF_INTEGRATION_PDF_ENTRY_ID to a known PDF entry to run "
            "this test."
        )

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
        pytest.skip(
            "Set LF_INTEGRATION_PDF_ENTRY_ID to a known PDF entry to run "
            "this test."
        )

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
