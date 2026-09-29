"""Tests for auth strategies."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import jwt as pyjwt
import pytest
from jwt.algorithms import ECAlgorithm
from pydantic import SecretStr
from pytest_httpx import HTTPXMock

from laserfiche_mcp.auth import (
    CloudServiceAppStrategy,
    OAuthClientCredentialsStrategy,
    PasswordGrantStrategy,
    _decode_cloud_access_key,
    build_auth_strategy,
)
from laserfiche_mcp.config import ApiVersion, Settings
from laserfiche_mcp.errors import LaserficheError

# Test vector taken verbatim from Laserfiche's own open-source client library
# (Laserfiche/lf-api-client-core-dotnet, tests/unit/JwtUtilsTests.cs) — a
# throwaway EC P-256 key pair used only in that project's own unit tests, not
# a real credential. Using it lets these tests prove the JWT this module
# produces is byte-for-byte compatible with what the official client emits,
# without needing a live Laserfiche Cloud tenant.
_TEST_JWK = {
    "kty": "EC",
    "crv": "P-256",
    "use": "sig",
    "kid": "TqlmmB_nwSb6Yyov9qIcJVCLdBAGhonC7C7s9kC4Avs",
    "x": "jdYj973SLwMIiuwA24TNXs1NmkvLeSzw-QBd_-_4-R8",
    "y": "wo3hyow9__af_4dIxsiL7Zs8oa2z4BTdS9LmX71Xj3w",
    "d": "qEnaazhXsBpePbV8MYLGz8NUnt4CKW7p0utPIj_NR2k",
}


def _access_key_b64(**overrides: object) -> str:
    payload: dict[str, object] = {
        "customerId": "cust-1",
        "domain": "laserfiche.com",
        "clientId": "ClientId",
        "jwk": _TEST_JWK,
    }
    payload.update(overrides)
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _public_key_from_test_jwk():  # type: ignore[no-untyped-def]
    public_jwk = {k: v for k, v in _TEST_JWK.items() if k != "d"}
    return ECAlgorithm(ECAlgorithm.SHA256).from_jwk(json.dumps(public_jwk))


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
async def test_password_grant_concurrent_calls_refresh_once(httpx_mock: HTTPXMock) -> None:
    """Two tool calls racing on a cold strategy must not double-refresh.

    Without the per-strategy asyncio.Lock, both concurrent ``apply()``
    calls observe ``self._access_token is None`` before either finishes
    its POST, and each independently exchanges a token.
    """
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
    req2 = httpx.Request("GET", "https://lf.example.test/api")
    await asyncio.gather(strategy.apply(req1), strategy.apply(req2))

    assert req1.headers["Authorization"] == "Bearer tok-1"
    assert req2.headers["Authorization"] == "Bearer tok-1"
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


# --- Cloud service-app strategy ----------------------------------------------
#
# Never verified against a live Laserfiche Cloud tenant (see auth.py's module
# docstring). What IS verified here, deterministically and without any
# network access: the assertion JWT this module builds is byte-for-byte
# compatible with Laserfiche's own official client library, using that
# library's own test vector — decode it with a public key derived from the
# same JWK and check header/claims match what CreateClientCredentialsAuthorizationJwt
# (Laserfiche/lf-api-client-core-dotnet) produces.


def test_decode_cloud_access_key_parses_valid_blob() -> None:
    decoded = _decode_cloud_access_key(_access_key_b64())
    assert decoded["clientId"] == "ClientId"
    assert decoded["domain"] == "laserfiche.com"
    assert decoded["jwk"]["kid"] == _TEST_JWK["kid"]


def test_decode_cloud_access_key_rejects_invalid_base64() -> None:
    with pytest.raises(ValueError, match="LF_CLOUD_ACCESS_KEY"):
        _decode_cloud_access_key("not-valid-base64!!!")


def test_decode_cloud_access_key_rejects_missing_fields() -> None:
    bad = base64.b64encode(json.dumps({"clientId": "x"}).encode()).decode()
    with pytest.raises(ValueError, match="domain"):
        _decode_cloud_access_key(bad)


def test_decode_cloud_access_key_rejects_jwk_missing_private_component() -> None:
    """A jwk with only the public point (no 'd') can't sign an assertion —
    must fail fast at startup, not on the first tool call."""
    public_only_jwk = {k: v for k, v in _TEST_JWK.items() if k != "d"}
    bad = _access_key_b64(jwk=public_only_jwk)
    with pytest.raises(ValueError, match="d"):
        _decode_cloud_access_key(bad)


def test_decode_cloud_access_key_rejects_wrong_curve() -> None:
    wrong_curve_jwk = {**_TEST_JWK, "crv": "P-384"}
    bad = _access_key_b64(jwk=wrong_curve_jwk)
    with pytest.raises(ValueError, match="P-256"):
        _decode_cloud_access_key(bad)


@pytest.mark.asyncio
async def test_cloud_strategy_assertion_jwt_matches_official_client_shape(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://signin.laserfiche.com/oauth/token",
        json={"access_token": "cloud-tok-1", "expires_in": 3600, "token_type": "bearer"},
    )

    strategy = CloudServiceAppStrategy(
        access_key_b64=_access_key_b64(),
        service_principal_key=SecretStr("ServicePrincipalKey"),
    )
    request = httpx.Request("GET", "https://api.laserfiche.com/repository/v2/x")
    await strategy.apply(request)

    assert request.headers["Authorization"] == "Bearer cloud-tok-1"

    token_request = httpx_mock.get_requests()[0]
    assertion = token_request.headers["Authorization"].removeprefix("Bearer ")

    header = pyjwt.get_unverified_header(assertion)
    assert header["alg"] == "ES256"
    assert header["kid"] == _TEST_JWK["kid"]

    claims = pyjwt.decode(
        assertion,
        _public_key_from_test_jwk(),
        algorithms=["ES256"],
        audience="laserfiche.com",
    )
    assert claims["client_id"] == "ClientId"
    assert claims["client_secret"] == "ServicePrincipalKey"
    assert claims["exp"] - claims["iat"] == 1800

    body = token_request.read().decode()
    assert "grant_type=client_credentials" in body


@pytest.mark.asyncio
async def test_cloud_strategy_posts_optional_scope(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://signin.laserfiche.com/oauth/token",
        json={"access_token": "cloud-tok-1", "expires_in": 3600},
    )
    strategy = CloudServiceAppStrategy(
        access_key_b64=_access_key_b64(),
        service_principal_key=SecretStr("spk"),
        scope="repository.Read repository.Write",
    )
    await strategy.apply(httpx.Request("GET", "https://api.laserfiche.com/x"))

    body = httpx_mock.get_requests()[0].read().decode()
    assert "scope=repository.Read+repository.Write" in body


@pytest.mark.asyncio
async def test_cloud_strategy_uses_regional_domain_for_token_url(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://signin.eu.laserfiche.com/oauth/token",
        json={"access_token": "eu-tok", "expires_in": 3600},
    )
    strategy = CloudServiceAppStrategy(
        access_key_b64=_access_key_b64(domain="eu.laserfiche.com"),
        service_principal_key=SecretStr("spk"),
    )
    request = httpx.Request("GET", "https://api.eu.laserfiche.com/repository/v2/x")
    await strategy.apply(request)
    assert request.headers["Authorization"] == "Bearer eu-tok"


@pytest.mark.asyncio
async def test_cloud_strategy_caches_token(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://signin.laserfiche.com/oauth/token",
        json={"access_token": "cloud-tok-1", "expires_in": 3600},
    )
    strategy = CloudServiceAppStrategy(
        access_key_b64=_access_key_b64(),
        service_principal_key=SecretStr("spk"),
    )
    await strategy.apply(httpx.Request("GET", "https://api.laserfiche.com/x"))
    await strategy.apply(httpx.Request("GET", "https://api.laserfiche.com/x"))

    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_cloud_strategy_refreshes_when_expired(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://signin.laserfiche.com/oauth/token",
        json={"access_token": "cloud-tok-1", "expires_in": 60},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://signin.laserfiche.com/oauth/token",
        json={"access_token": "cloud-tok-2", "expires_in": 60},
    )
    strategy = CloudServiceAppStrategy(
        access_key_b64=_access_key_b64(),
        service_principal_key=SecretStr("spk"),
    )

    req1 = httpx.Request("GET", "https://api.laserfiche.com/x")
    await strategy.apply(req1)
    assert req1.headers["Authorization"] == "Bearer cloud-tok-1"

    strategy._expires_at = time.time() - 1

    req2 = httpx.Request("GET", "https://api.laserfiche.com/x")
    await strategy.apply(req2)
    assert req2.headers["Authorization"] == "Bearer cloud-tok-2"
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_cloud_strategy_wraps_rejected_credentials_as_laserfiche_error(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://signin.laserfiche.com/oauth/token",
        status_code=401,
        json={"error": "invalid_client"},
    )
    strategy = CloudServiceAppStrategy(
        access_key_b64=_access_key_b64(),
        service_principal_key=SecretStr("spk"),
    )
    with pytest.raises(LaserficheError) as excinfo:
        await strategy.apply(httpx.Request("GET", "https://api.laserfiche.com/x"))
    assert excinfo.value.status_code == 401


def test_build_auth_strategy_cloud(lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("LF_AUTH_MODE", "api_key")
    monkeypatch.setenv("LF_API_VERSION", "v2")
    monkeypatch.setenv("LF_CLOUD_ACCESS_KEY", _access_key_b64())
    monkeypatch.setenv("LF_CLOUD_SERVICE_PRINCIPAL_KEY", "spk")
    monkeypatch.delenv("LF_USERNAME", raising=False)
    monkeypatch.delenv("LF_PASSWORD", raising=False)

    settings = Settings()  # type: ignore[call-arg]
    strategy = build_auth_strategy(settings)
    assert isinstance(strategy, CloudServiceAppStrategy)
