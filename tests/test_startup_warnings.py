"""Startup warnings for legal-but-risky configurations, and JWKS URL validation."""

from __future__ import annotations

import httpx
import pytest

from laserfiche_mcp.cli import risky_config_warnings
from laserfiche_mcp.config import Settings
from laserfiche_mcp.oauth import JwtTokenVerifier, validate_jwks_uri


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "repo_api_url": "https://lf.example.com/LFRepositoryAPI",
        "repository_id": "r",
        "username": "u",
        "password": "p",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


# --- import fence ------------------------------------------------------------


def test_no_warning_in_default_read_only_mode() -> None:
    assert risky_config_warnings(_settings()) == []


def test_warns_when_writes_on_and_import_fence_unset() -> None:
    warnings = risky_config_warnings(_settings(read_only=False))
    assert len(warnings) == 1
    assert "LF_IMPORT_SOURCE_DIRS" in warnings[0]
    assert "import_document" in warnings[0]


def test_no_warning_when_import_fence_is_set() -> None:
    assert (
        risky_config_warnings(_settings(read_only=False, import_source_dirs="/srv/imports")) == []
    )


def test_star_fence_is_an_explicit_choice_and_silences_the_warning() -> None:
    assert risky_config_warnings(_settings(read_only=False, import_source_dirs="*")) == []


def test_unset_warning_explains_deprecation_and_the_opt_out() -> None:
    (warning,) = risky_config_warnings(_settings(read_only=False))
    assert "deprecated" in warning and "LF_IMPORT_SOURCE_DIRS=*" in warning


def test_no_import_warning_when_import_tool_is_not_allowed() -> None:
    settings = _settings(read_only=False, write_tools_allowed="merge_fields,merge_tags")
    assert risky_config_warnings(settings) == []


def test_import_warning_when_allowlist_names_v2_import_tool() -> None:
    settings = _settings(read_only=False, write_tools_allowed="laserfiche_document_import")
    assert any("LF_IMPORT_SOURCE_DIRS" in w for w in risky_config_warnings(settings))


# --- plain http --------------------------------------------------------------


def test_warns_on_plain_http_api_url() -> None:
    warnings = risky_config_warnings(
        _settings(repo_api_url="http://gc-its-dm-repo/LFRepositoryAPI")
    )
    assert len(warnings) == 1
    assert "plain http://" in warnings[0] and "gc-its-dm-repo" in warnings[0]


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_no_http_warning_for_loopback(host: str) -> None:
    assert (
        risky_config_warnings(_settings(repo_api_url=f"http://{host}:8080/LFRepositoryAPI")) == []
    )


def test_warns_on_plain_http_oauth_token_url() -> None:
    settings = _settings(
        auth_mode="oauth",
        oauth_token_url="http://idp.internal/token",
        client_id="c",
        client_secret="s",
    )
    assert any("LF_OAUTH_TOKEN_URL" in w for w in risky_config_warnings(settings))


# --- jwks_uri validation ------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "https://login.example.com/keys",
        "https://www.googleapis.com/oauth2/v3/certs",  # different host than issuer is fine
        "http://localhost:9000/keys",
        "http://127.0.0.1/keys",
    ],
)
def test_jwks_uri_accepted(uri: str) -> None:
    assert validate_jwks_uri(uri, source="test") == uri


@pytest.mark.parametrize(
    "uri",
    [
        "http://login.example.com/keys",  # plaintext off-box
        "ftp://login.example.com/keys",
        "https://user:pw@login.example.com/keys",
        "https:///keys",
        "not a url",
        "",
        None,
        12345,
    ],
)
def test_jwks_uri_rejected(uri: object) -> None:
    with pytest.raises(ValueError):
        validate_jwks_uri(uri, source="test")


async def test_discovery_with_insecure_jwks_uri_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jwks_uri": "http://evil.example.net/keys"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    verifier = JwtTokenVerifier(
        issuer="https://idp.example.com", audience="aud", algorithms=["RS256"]
    )
    with pytest.raises(ValueError, match="https"):
        await verifier._jwks_url()


async def test_discovery_with_missing_jwks_uri_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issuer": "https://idp.example.com"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    verifier = JwtTokenVerifier(
        issuer="https://idp.example.com", audience="aud", algorithms=["RS256"]
    )
    with pytest.raises(ValueError, match="jwks_uri"):
        await verifier._jwks_url()


async def test_insecure_jwks_uri_means_token_is_rejected_not_crashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jwks_uri": "http://evil.example.net/keys"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    verifier = JwtTokenVerifier(
        issuer="https://idp.example.com", audience="aud", algorithms=["RS256"]
    )
    assert await verifier.verify_token("a.b.c") is None


# --- OAuth issuer / JWKS URL must be https -------------------------------------


def _oauth(**overrides: object) -> Settings:
    return _settings(
        http_oauth_issuer="https://login.example.com/tenant/v2.0",
        http_public_url="https://mcp.example.org/mcp",
        **overrides,
    )


def test_https_issuer_is_accepted() -> None:
    assert _oauth().oauth_enabled is True


@pytest.mark.parametrize("issuer", ["http://login.example.com", "http://10.0.0.5/idp"])
def test_plain_http_issuer_is_refused_at_startup(issuer: str) -> None:
    with pytest.raises(ValueError, match="LF_HTTP_OAUTH_ISSUER.*https"):
        _settings(http_oauth_issuer=issuer, http_public_url="https://mcp.example.org/mcp")


def test_loopback_http_issuer_is_allowed_for_local_testing() -> None:
    settings = _settings(
        http_oauth_issuer="http://localhost:9000", http_public_url="https://mcp.example.org/mcp"
    )
    assert settings.oauth_enabled


def test_plain_http_explicit_jwks_url_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="LF_HTTP_OAUTH_JWKS_URL.*https"):
        _oauth(http_oauth_jwks_url="http://login.example.com/keys")


def test_https_explicit_jwks_url_on_another_host_is_fine() -> None:
    _oauth(http_oauth_jwks_url="https://www.googleapis.com/oauth2/v3/certs")


def test_idp_verification_ignores_the_laserfiche_verify_ssl_switch() -> None:
    """LF_VERIFY_SSL=false is about the Laserfiche server; it must not also switch off
    certificate checking for the identity provider that signs access tokens."""
    verifier = JwtTokenVerifier(
        issuer="https://idp.example.com", audience="a", algorithms=["RS256"], verify=False
    )
    assert verifier._verify is True


def test_idp_verification_honours_the_internal_ca_context() -> None:
    import ssl

    ctx = ssl.create_default_context()
    verifier = JwtTokenVerifier(
        issuer="https://idp.example.com", audience="a", algorithms=["RS256"], verify=ctx
    )
    assert verifier._verify is ctx
