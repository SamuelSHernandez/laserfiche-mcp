"""Tests for auth strategies."""

from __future__ import annotations

import time

import httpx
import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from laserfiche_mcp.auth import (
    OAuthClientCredentialsStrategy,
    PasswordGrantStrategy,
    build_auth_strategy,
)
from laserfiche_mcp.config import ApiVersion, Settings
from laserfiche_mcp.errors import LaserficheError


@pytest.mark.asyncio
async def test_password_grant_exchanges_creds_for_bearer(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories/demo/Token",
        json={"access_token": "tok-1", "expires_in": 900, "token_type": "bearer"},
    )

    strategy = PasswordGrantStrategy(
        base_url="https://lf.example.test/LFRepositoryAPI/",
        repository_id="demo",
        username="svc",
        password=SecretStr("secret"),
    )

    request = httpx.Request(
        "GET", "https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo/Entries/1"
    )
    await strategy.apply(request)

    assert request.headers["Authorization"] == "Bearer tok-1"

    # Token request used form encoding with grant_type=password
    token_request = httpx_mock.get_requests()[0]
    body = token_request.read().decode()
    assert "grant_type=password" in body
    assert "username=svc" in body
    assert "password=secret" in body


@pytest.mark.asyncio
async def test_password_grant_caches_token(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories/demo/Token",
        json={"access_token": "tok-1", "expires_in": 900},
    )

    strategy = PasswordGrantStrategy(
        base_url="https://lf.example.test/LFRepositoryAPI/",
        repository_id="demo",
        username="svc",
        password=SecretStr("secret"),
    )

    req1 = httpx.Request("GET", "https://lf.example.test/api")
    await strategy.apply(req1)
    req2 = httpx.Request("GET", "https://lf.example.test/api")
    await strategy.apply(req2)

    assert req1.headers["Authorization"] == "Bearer tok-1"
    assert req2.headers["Authorization"] == "Bearer tok-1"
    # Only one token exchange, not two
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_password_grant_refreshes_when_expired(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories/demo/Token",
        json={"access_token": "tok-1", "expires_in": 60},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories/demo/Token",
        json={"access_token": "tok-2", "expires_in": 60},
    )

    strategy = PasswordGrantStrategy(
        base_url="https://lf.example.test/LFRepositoryAPI/",
        repository_id="demo",
        username="svc",
        password=SecretStr("secret"),
    )

    req1 = httpx.Request("GET", "https://lf.example.test/api")
    await strategy.apply(req1)
    assert req1.headers["Authorization"] == "Bearer tok-1"

    # Force expiry
    strategy._expires_at = time.time() - 1

    req2 = httpx.Request("GET", "https://lf.example.test/api")
    await strategy.apply(req2)
    assert req2.headers["Authorization"] == "Bearer tok-2"
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_password_grant_v2_token_url(httpx_mock: HTTPXMock) -> None:
    """When api_version=v2 is passed, the token endpoint moves to /v2/..."""
    httpx_mock.add_response(
        method="POST",
        url="https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo/Token",
        json={"access_token": "tok-v2", "expires_in": 900},
    )

    strategy = PasswordGrantStrategy(
        base_url="https://lf.example.test/LFRepositoryAPI/",
        repository_id="demo",
        username="svc",
        password=SecretStr("secret"),
        api_version=ApiVersion.V2,
    )

    request = httpx.Request("GET", "https://lf.example.test/api")
    await strategy.apply(request)
    assert request.headers["Authorization"] == "Bearer tok-v2"


@pytest.mark.asyncio
async def test_oauth_client_credentials_token_exchange(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://lfds.example.test/oauth/token",
        json={"access_token": "tok-1", "expires_in": 3600},
    )

    strategy = OAuthClientCredentialsStrategy(
        token_url="https://lfds.example.test/oauth/token",
        client_id="cid",
        client_secret=SecretStr("csec"),
    )

    request = httpx.Request("GET", "https://lf.example.test/api")
    await strategy.apply(request)
    assert request.headers["Authorization"] == "Bearer tok-1"


def test_build_auth_strategy_password(lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    strategy = build_auth_strategy(settings)
    assert isinstance(strategy, PasswordGrantStrategy)


def test_build_auth_strategy_oauth(lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_AUTH_MODE", "oauth")
    monkeypatch.delenv("LF_USERNAME", raising=False)
    monkeypatch.delenv("LF_PASSWORD", raising=False)
    monkeypatch.setenv("LF_OAUTH_TOKEN_URL", "https://lfds.example.test/oauth/token")
    monkeypatch.setenv("LF_CLIENT_ID", "cid")
    monkeypatch.setenv("LF_CLIENT_SECRET", "csec")

    settings = Settings()  # type: ignore[call-arg]
    strategy = build_auth_strategy(settings)
    assert isinstance(strategy, OAuthClientCredentialsStrategy)


# --- Token-exchange failures must honor the structured error contract --------
#
# ``AuthStrategy.apply`` runs *inside* every tool's ``except LaserficheError``
# block. Anything else raised here escapes that handler entirely and reaches
# the model as a bare "Error executing tool <name>:" with an empty message —
# no slug, no hint, nothing to act on.


_TOKEN_URL = "https://lf.example.test/LFRepositoryAPI/v1/Repositories/demo/Token"


def _password_strategy() -> PasswordGrantStrategy:
    return PasswordGrantStrategy(
        base_url="https://lf.example.test/LFRepositoryAPI/",
        repository_id="demo",
        username="svc",
        password=SecretStr("secret"),
    )


@pytest.mark.asyncio
async def test_password_grant_wraps_timeout_as_laserfiche_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=_TOKEN_URL)

    request = httpx.Request("GET", "https://lf.example.test/LFRepositoryAPI/v1/x")
    with pytest.raises(LaserficheError) as excinfo:
        await _password_strategy().apply(request)

    assert "Timed out" in str(excinfo.value)


@pytest.mark.asyncio
async def test_password_grant_wraps_connect_error_as_laserfiche_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("no route to host"), url=_TOKEN_URL)

    request = httpx.Request("GET", "https://lf.example.test/LFRepositoryAPI/v1/x")
    with pytest.raises(LaserficheError) as excinfo:
        await _password_strategy().apply(request)

    assert "LF_REPO_API_URL" in str(excinfo.value)


@pytest.mark.asyncio
async def test_password_grant_wraps_rejected_credentials_with_status(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_TOKEN_URL,
        status_code=401,
        json={"errorCode": 9010, "title": "Invalid credentials"},
    )

    request = httpx.Request("GET", "https://lf.example.test/LFRepositoryAPI/v1/x")
    with pytest.raises(LaserficheError) as excinfo:
        await _password_strategy().apply(request)

    # status_code and detail must survive so classify_lf_error can map this
    # onto the auth_failed slug rather than a generic upstream failure.
    assert excinfo.value.status_code == 401
    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["errorCode"] == 9010


@pytest.mark.asyncio
async def test_password_grant_wraps_payload_without_access_token(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(method="POST", url=_TOKEN_URL, json={"unexpected": "shape"})

    request = httpx.Request("GET", "https://lf.example.test/LFRepositoryAPI/v1/x")
    with pytest.raises(LaserficheError) as excinfo:
        await _password_strategy().apply(request)

    assert "access_token" in str(excinfo.value)


@pytest.mark.asyncio
async def test_oauth_client_credentials_defaults_to_one_hour_expiry(
    httpx_mock: HTTPXMock,
) -> None:
    """The two grants carry different fallback lifetimes; don't collapse them."""
    httpx_mock.add_response(
        method="POST",
        url="https://lfds.example.test/oauth/token",
        json={"access_token": "tok-1"},
    )

    strategy = OAuthClientCredentialsStrategy(
        token_url="https://lfds.example.test/oauth/token",
        client_id="cid",
        client_secret=SecretStr("csecret"),
    )
    before = time.time()
    await strategy.apply(httpx.Request("GET", "https://lf.example.test/x"))

    assert strategy._expires_at >= before + 3500
