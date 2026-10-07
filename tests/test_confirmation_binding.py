"""Confirmation tokens are bound to the caller and to the entry state previewed.

Together with the existing entry/name/parameter bindings these make a token
self-defeating on replay without any server-side bookkeeping: executing the
operation changes the entry, which invalidates the token that authorized it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mcp.server.auth.provider import AccessToken
from pytest_httpx import HTTPXMock

from laserfiche_mcp import confirmation, server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

# --- unit level ---------------------------------------------------------------


def _as_caller(
    monkeypatch: pytest.MonkeyPatch, subject: str | None, client_id: str = "app"
) -> None:
    """Make the SDK's per-request auth context report this caller (None = anonymous)."""
    token = (
        None
        if subject is None
        else AccessToken(token="t", client_id=client_id, scopes=[], subject=subject)
    )
    monkeypatch.setattr("mcp.server.auth.middleware.auth_context.get_access_token", lambda: token)


def test_a_token_is_bound_to_the_caller_who_requested_the_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_caller(monkeypatch, "alice")
    token = confirmation.create_token("delete_entry", 5, "Doc", version="T1")

    assert confirmation.verify_token(token, "delete_entry", 5, "Doc", version="T1")[0]

    _as_caller(monkeypatch, "bob")
    ok, reason = confirmation.verify_token(token, "delete_entry", 5, "Doc", version="T1")
    assert ok is False
    assert "different caller" in (reason or "")


def test_the_same_oauth_client_but_a_different_user_is_a_different_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_caller(monkeypatch, "alice", client_id="shared-connector")
    token = confirmation.create_token("delete_entry", 5, "Doc")
    _as_caller(monkeypatch, "carol", client_id="shared-connector")
    assert not confirmation.verify_token(token, "delete_entry", 5, "Doc")[0]


def test_an_anonymous_token_is_not_valid_for_an_identified_caller_and_vice_versa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_caller(monkeypatch, None)
    anon = confirmation.create_token("delete_entry", 5, "Doc")
    assert confirmation.verify_token(anon, "delete_entry", 5, "Doc")[0]

    _as_caller(monkeypatch, "alice")
    assert not confirmation.verify_token(anon, "delete_entry", 5, "Doc")[0]
    named = confirmation.create_token("delete_entry", 5, "Doc")
    _as_caller(monkeypatch, None)
    assert not confirmation.verify_token(named, "delete_entry", 5, "Doc")[0]


def test_falls_back_to_client_id_when_the_sdk_token_has_no_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mcp.server.auth.middleware.auth_context.get_access_token",
        lambda: AccessToken(token="t", client_id="svc-account", scopes=[]),
    )
    token = confirmation.create_token("delete_entry", 5, "Doc")
    assert confirmation.verify_token(token, "delete_entry", 5, "Doc")[0]
    monkeypatch.setattr(
        "mcp.server.auth.middleware.auth_context.get_access_token",
        lambda: AccessToken(token="t", client_id="other-service", scopes=[]),
    )
    assert not confirmation.verify_token(token, "delete_entry", 5, "Doc")[0]


def test_a_token_is_bound_to_the_entry_state_that_was_previewed() -> None:
    token = confirmation.create_token("delete_entry", 5, "Doc", version="2026-10-07T10:00")
    assert confirmation.verify_token(token, "delete_entry", 5, "Doc", version="2026-10-07T10:00")[0]

    ok, reason = confirmation.verify_token(
        token, "delete_entry", 5, "Doc", version="2026-10-07T10:05"
    )
    assert ok is False
    assert "changed since the preview" in (reason or "")


def test_a_missing_version_degrades_gracefully_not_catastrophically() -> None:
    # A server that doesn't report a modified time: the other bindings still apply.
    token = confirmation.create_token("delete_entry", 5, "Doc", version=None)
    assert confirmation.verify_token(token, "delete_entry", 5, "Doc", version=None)[0]
    assert not confirmation.verify_token(token, "delete_entry", 6, "Doc", version=None)[0]


def test_tampering_with_the_caller_or_version_segment_breaks_the_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    _as_caller(monkeypatch, "alice")
    token = confirmation.create_token("delete_entry", 5, "Doc", version="T1")
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode().split(":")
    raw[3] = "-"  # strip the caller binding
    forged = base64.urlsafe_b64encode(":".join(raw).encode()).decode().rstrip("=")
    ok, reason = confirmation.verify_token(forged, "delete_entry", 5, "Doc", version="T1")
    assert ok is False and "signature" in (reason or "").lower()


# --- tool level: the destructive operations --------------------------------------


def _serve_entry(
    httpx_mock: HTTPXMock, state: dict[str, Any], entry_id: int = 42, **extra: Any
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = {
            "id": entry_id,
            "name": "Doomed",
            "entryType": "Document",
            "lastModifiedTime": state["version"],
            **extra,
        }
        return httpx.Response(200, json=body)

    httpx_mock.add_callback(
        respond, method="GET", url=f"{_BASE}/Entries/{entry_id}", is_reusable=True
    )


@pytest.mark.asyncio
async def test_delete_entry_token_cannot_be_replayed_after_it_executes(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """The argument against needing single-use: executing modifies the entry, so the
    token that authorized it no longer matches and the second call is refused BEFORE
    anything is sent to the server."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    state = {"version": "T1"}
    _serve_entry(httpx_mock, state)
    httpx_mock.add_response(
        method="DELETE",
        url=f"{_BASE}/Entries/42",
        status_code=202,
        json={"token": "op-1", "taskId": "t-1"},
    )

    preview = await server.delete_entry(42)
    token = preview["confirmation_token"]
    assert (await server.delete_entry(42, confirmation_token=token))["mode"] == "executed"

    state["version"] = "T2"  # the server touched the entry as part of the delete
    replay = await server.delete_entry(42, confirmation_token=token)

    assert replay["mode"] == "error"
    assert replay["error"] == "invalid_confirmation_token"
    assert "changed since" in replay["reason"]
    assert len([r for r in httpx_mock.get_requests() if r.method == "DELETE"]) == 1


@pytest.mark.asyncio
async def test_delete_entry_refuses_if_the_entry_changed_between_preview_and_execute(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """TOCTOU: the user confirmed the entry as it WAS. If someone edited it since,
    they must see it again."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    state = {"version": "T1"}
    _serve_entry(httpx_mock, state)

    token = (await server.delete_entry(42))["confirmation_token"]
    state["version"] = "T9"  # someone edited the document after the preview
    result = await server.delete_entry(42, confirmation_token=token)

    assert result["error"] == "invalid_confirmation_token"
    assert not [r for r in httpx_mock.get_requests() if r.method == "DELETE"]


@pytest.mark.asyncio
async def test_delete_entry_preview_by_one_user_cannot_be_executed_by_another(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    state = {"version": "T1"}
    _serve_entry(httpx_mock, state)

    _as_caller(monkeypatch, "agent-service")
    token = (await server.delete_entry(42))["confirmation_token"]

    _as_caller(monkeypatch, "human-admin")
    result = await server.delete_entry(42, confirmation_token=token)

    assert result["error"] == "invalid_confirmation_token"
    assert "different caller" in result["reason"]
    assert not [r for r in httpx_mock.get_requests() if r.method == "DELETE"]


@pytest.mark.asyncio
async def test_delete_edoc_token_is_bound_to_the_entry_state(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """delete_edoc was the operation where a replay could otherwise hit a document
    re-attached after the first delete; the version binding closes that."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    state = {"version": "T1"}
    _serve_entry(httpx_mock, state)

    token = (await server.delete_edoc(42))["confirmation_token"]
    state["version"] = "T2"  # edoc re-attached / entry modified since
    result = await server.delete_edoc(42, confirmation_token=token)

    assert result["error"] == "invalid_confirmation_token"
    assert not [r for r in httpx_mock.get_requests() if r.method == "DELETE"]
