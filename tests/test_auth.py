"""Tests for auth strategies."""

from __future__ import annotations

import base64
import time

import httpx
import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from laserfiche_mcp.auth import BasicAuthStrategy, OAuthStrategy, build_auth_strategy
from laserfiche_mcp.config import Settings


@pytest.mark.asyncio
async def test_basic_auth_sets_authorization_header() -> None:
    strategy = BasicAuthStrategy("alice", SecretStr("p4ssw0rd"))
    request = httpx.Request("GET", "https://lf.example.test/foo")
    await strategy.apply(request)

    expected = "Basic " + base64.b64encode(b"alice:p4ssw0rd").decode()
    assert request.headers["Authorization"] == expected


@pytest.mark.asyncio
async def test_oauth_fetches_and_caches_token(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://lfds.example.test/oauth/token",
        json={"access_token": "tok-1", "expires_in": 3600},
    )

    strategy = OAuthStrategy(
        token_url="https://lfds.example.test/oauth/token",
        client_id="cid",
        client_secret=SecretStr("csec"),
    )

    req1 = httpx.Request("GET", "https://lf.example.test/api")
    await strategy.apply(req1)
    assert req1.headers["Authorization"] == "Bearer tok-1"

    # Second call within expiry must NOT re-fetch
    req2 = httpx.Request("GET", "https://lf.example.test/api")
    await strategy.apply(req2)
    assert req2.headers["Authorization"] == "Bearer tok-1"
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_oauth_refreshes_when_expired(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://lfds.example.test/oauth/token",
        json={"access_token": "tok-1", "expires_in": 60},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://lfds.example.test/oauth/token",
        json={"access_token": "tok-2", "expires_in": 60},
    )

    strategy = OAuthStrategy(
        token_url="https://lfds.example.test/oauth/token",
        client_id="cid",
        client_secret=SecretStr("csec"),
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


def test_build_auth_strategy_basic(lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    strategy = build_auth_strategy(settings)
    assert isinstance(strategy, BasicAuthStrategy)


def test_build_auth_strategy_oauth(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_AUTH_MODE", "oauth")
    monkeypatch.delenv("LF_USERNAME", raising=False)
    monkeypatch.delenv("LF_PASSWORD", raising=False)
    monkeypatch.setenv("LF_OAUTH_TOKEN_URL", "https://lfds.example.test/oauth/token")
    monkeypatch.setenv("LF_CLIENT_ID", "cid")
    monkeypatch.setenv("LF_CLIENT_SECRET", "csec")

    settings = Settings()  # type: ignore[call-arg]
    strategy = build_auth_strategy(settings)
    assert isinstance(strategy, OAuthStrategy)
