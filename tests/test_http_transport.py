"""Tests for http_transport.py — the Streamable HTTP transport wiring.

The blocking ``run_http`` (uvicorn) is not exercised here; we test the
testable pieces: loopback classification, the static bearer-token middleware,
and ``build_http_app`` (host/port/path binding, auth guard, exposure warning).
"""

from __future__ import annotations

import logging

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from laserfiche_mcp import http_transport
from laserfiche_mcp.config import Settings

# --- is_loopback -------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_is_loopback_true(host: str) -> None:
    assert http_transport.is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "lf.example.com"])
def test_is_loopback_false(host: str) -> None:
    assert http_transport.is_loopback(host) is False


# --- bearer-token middleware -------------------------------------------------


def _client_with_token(token: str) -> TestClient:
    """A minimal app guarded by the bearer middleware, returning 'ok' at /."""
    middleware = http_transport._build_auth_middleware(token)
    app = Starlette(routes=[Route("/", lambda _req: PlainTextResponse("ok"))])
    app.add_middleware(middleware)
    return TestClient(app)


def test_bearer_middleware_allows_correct_token() -> None:
    client = _client_with_token("letmein")
    resp = client.get("/", headers={"Authorization": "Bearer letmein"})
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_bearer_middleware_rejects_missing_header() -> None:
    client = _client_with_token("letmein")
    resp = client.get("/")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_bearer_middleware_rejects_wrong_token() -> None:
    client = _client_with_token("letmein")
    resp = client.get("/", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_bearer_middleware_rejects_wrong_scheme() -> None:
    client = _client_with_token("letmein")
    resp = client.get("/", headers={"Authorization": "Basic letmein"})
    assert resp.status_code == 401


# --- build_http_app ----------------------------------------------------------


def test_build_http_app_binds_settings(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("LF_HTTP_PORT", "8123")
    monkeypatch.setenv("LF_HTTP_PATH", "/laserfiche")
    settings = Settings()  # type: ignore[call-arg]

    app = http_transport.build_http_app(settings)
    assert isinstance(app, Starlette)

    from laserfiche_mcp import _app

    assert _app.mcp.settings.host == "127.0.0.1"
    assert _app.mcp.settings.port == 8123
    assert _app.mcp.settings.streamable_http_path == "/laserfiche"


def test_build_http_app_warns_when_exposed_without_token(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LF_HTTP_HOST", "0.0.0.0")
    monkeypatch.delenv("LF_HTTP_AUTH_TOKEN", raising=False)
    settings = Settings()  # type: ignore[call-arg]

    with caplog.at_level(logging.WARNING, logger="laserfiche_mcp"):
        http_transport.build_http_app(settings)

    assert any(
        "NO authentication" in rec.message and "0.0.0.0" in rec.message for rec in caplog.records
    )


def test_build_http_app_no_warning_on_loopback(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LF_HTTP_HOST", "127.0.0.1")
    monkeypatch.delenv("LF_HTTP_AUTH_TOKEN", raising=False)
    settings = Settings()  # type: ignore[call-arg]

    with caplog.at_level(logging.WARNING, logger="laserfiche_mcp"):
        http_transport.build_http_app(settings)

    assert not any("NO authentication" in rec.message for rec in caplog.records)


def test_build_http_app_no_warning_when_exposed_with_token(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LF_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("LF_HTTP_AUTH_TOKEN", "s3cret")
    settings = Settings()  # type: ignore[call-arg]

    with caplog.at_level(logging.WARNING, logger="laserfiche_mcp"):
        http_transport.build_http_app(settings)

    assert not any("NO authentication" in rec.message for rec in caplog.records)


# --- build_http_app: OAuth mode ----------------------------------------------


def test_build_http_app_oauth_sets_auth_and_verifier(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_OAUTH_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("LF_HTTP_PUBLIC_URL", "https://lf.example.com/mcp")
    settings = Settings()  # type: ignore[call-arg]

    http_transport.build_http_app(settings)

    from laserfiche_mcp import _app
    from laserfiche_mcp.oauth import JwtTokenVerifier

    assert _app.mcp.settings.auth is not None
    assert str(_app.mcp.settings.auth.issuer_url).rstrip("/") == "https://idp.example.com"
    assert isinstance(_app.mcp._token_verifier, JwtTokenVerifier)

    # Reset shared singleton so later tests aren't affected.
    _app.mcp.settings.auth = None
    _app.mcp._token_verifier = None


def test_build_http_app_oauth_takes_precedence_over_static_token(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_OAUTH_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("LF_HTTP_PUBLIC_URL", "https://lf.example.com/mcp")
    monkeypatch.setenv("LF_HTTP_AUTH_TOKEN", "static-token-ignored")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.oauth_enabled is True

    http_transport.build_http_app(settings)

    from laserfiche_mcp import _app

    # OAuth wins: FastMCP auth is configured (not the static-token middleware path).
    assert _app.mcp.settings.auth is not None
    _app.mcp.settings.auth = None
    _app.mcp._token_verifier = None


def test_build_http_app_static_token_clears_fastmcp_auth(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_AUTH_TOKEN", "tok")
    monkeypatch.delenv("LF_HTTP_OAUTH_ISSUER", raising=False)
    settings = Settings()  # type: ignore[call-arg]

    http_transport.build_http_app(settings)

    from laserfiche_mcp import _app

    assert _app.mcp.settings.auth is None
