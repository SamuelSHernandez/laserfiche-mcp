"""Tests for the Settings model."""

from __future__ import annotations

import pytest

from laserfiche_mcp.config import ApiVersion, AuthMode, DeploymentMode, Settings


def test_loads_password_self_hosted(lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    assert settings.deployment_mode is DeploymentMode.SELF_HOSTED
    assert settings.auth_mode is AuthMode.PASSWORD
    assert settings.read_only is True
    assert settings.password is not None
    assert "secret" not in repr(settings.password)


def test_api_version_defaults_to_v1(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When LF_API_VERSION is unset, the default must be v1."""
    monkeypatch.delenv("LF_API_VERSION", raising=False)
    settings = Settings()  # type: ignore[call-arg]
    assert settings.api_version is ApiVersion.V1


def test_api_version_v2_parsed(lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_API_VERSION", "v2")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.api_version is ApiVersion.V2


def test_api_version_invalid_rejected(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_API_VERSION", "v3")
    with pytest.raises(ValueError):
        Settings()  # type: ignore[call-arg]


def test_missing_password_for_password_mode_raises(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LF_PASSWORD")
    with pytest.raises(ValueError, match="LF_PASSWORD"):
        Settings()  # type: ignore[call-arg]


def test_oauth_requires_token_url_and_client(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_AUTH_MODE", "oauth")
    monkeypatch.delenv("LF_USERNAME", raising=False)
    monkeypatch.delenv("LF_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="LF_OAUTH_TOKEN_URL"):
        Settings()  # type: ignore[call-arg]


def test_oauth_succeeds_with_full_config(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_AUTH_MODE", "oauth")
    monkeypatch.delenv("LF_USERNAME", raising=False)
    monkeypatch.delenv("LF_PASSWORD", raising=False)
    monkeypatch.setenv("LF_OAUTH_TOKEN_URL", "https://lfds.example.test/oauth/token")
    monkeypatch.setenv("LF_CLIENT_ID", "client-abc")
    monkeypatch.setenv("LF_CLIENT_SECRET", "secret-def")

    settings = Settings()  # type: ignore[call-arg]
    assert settings.auth_mode is AuthMode.OAUTH
    assert settings.client_id == "client-abc"


def test_api_key_mode_rejected(lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_AUTH_MODE", "api_key")
    with pytest.raises(NotImplementedError, match="api_key"):
        Settings()  # type: ignore[call-arg]


def test_cloud_mode_rejected(lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_DEPLOYMENT_MODE", "cloud")
    with pytest.raises(NotImplementedError, match="Cloud"):
        Settings()  # type: ignore[call-arg]


def test_invalid_log_level(lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_LOG_LEVEL", "VERBOSE")
    with pytest.raises(ValueError, match="LF_LOG_LEVEL"):
        Settings()  # type: ignore[call-arg]


def test_max_results_default_must_not_exceed_ceiling(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_MAX_RESULTS_DEFAULT", "100")
    monkeypatch.setenv("LF_MAX_RESULTS_CEILING", "50")
    with pytest.raises(ValueError, match="MAX_RESULTS"):
        Settings()  # type: ignore[call-arg]


def test_invalid_url_raises(lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_REPO_API_URL", "not-a-url")
    with pytest.raises(ValueError):
        Settings()  # type: ignore[call-arg]


# --- HTTP transport (remote / web clients) -----------------------------------


def test_http_defaults_are_loopback(lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    assert settings.http_host == "127.0.0.1"
    assert settings.http_port == 8000
    assert settings.http_path == "/mcp"
    assert settings.http_auth_token is None


def test_http_overrides_from_env(lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("LF_HTTP_PORT", "9443")
    monkeypatch.setenv("LF_HTTP_PATH", "/laserfiche")
    monkeypatch.setenv("LF_HTTP_AUTH_TOKEN", "s3cret")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.http_host == "0.0.0.0"
    assert settings.http_port == 9443
    assert settings.http_path == "/laserfiche"
    assert settings.http_auth_token is not None
    assert settings.http_auth_token.get_secret_value() == "s3cret"
    # The token must not leak through repr.
    assert "s3cret" not in repr(settings.http_auth_token)


def test_http_path_must_start_with_slash(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_PATH", "mcp")
    with pytest.raises(ValueError, match="LF_HTTP_PATH"):
        Settings()  # type: ignore[call-arg]


def test_http_port_out_of_range_rejected(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_PORT", "70000")
    with pytest.raises(ValueError):
        Settings()  # type: ignore[call-arg]


# --- OAuth Resource Server -----------------------------------------------------


def test_oauth_disabled_by_default(lf_env: dict[str, str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    assert settings.oauth_enabled is False
    assert settings.http_oauth_issuer is None
    assert settings.oauth_effective_audience is None


def test_oauth_requires_public_url(lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_HTTP_OAUTH_ISSUER", "https://idp.example.com")
    with pytest.raises(ValueError, match="LF_HTTP_PUBLIC_URL"):
        Settings()  # type: ignore[call-arg]


def test_oauth_enabled_with_issuer_and_public_url(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_OAUTH_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("LF_HTTP_PUBLIC_URL", "https://lf.example.com/mcp")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.oauth_enabled is True
    # Audience defaults to the public URL when not set explicitly.
    assert settings.oauth_effective_audience == "https://lf.example.com/mcp"
    assert settings.oauth_algorithms == ["RS256"]
    assert settings.oauth_required_scopes == []


def test_oauth_explicit_audience_and_scopes(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_OAUTH_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("LF_HTTP_PUBLIC_URL", "https://lf.example.com/mcp")
    monkeypatch.setenv("LF_HTTP_OAUTH_AUDIENCE", "api://laserfiche")
    monkeypatch.setenv("LF_HTTP_OAUTH_REQUIRED_SCOPES", "laserfiche.read, laserfiche.write")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.oauth_effective_audience == "api://laserfiche"
    assert settings.oauth_required_scopes == ["laserfiche.read", "laserfiche.write"]


def test_oauth_rejects_symmetric_algorithm(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_OAUTH_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("LF_HTTP_PUBLIC_URL", "https://lf.example.com/mcp")
    monkeypatch.setenv("LF_HTTP_OAUTH_ALGORITHMS", "HS256")
    with pytest.raises(ValueError, match="asymmetric"):
        Settings()  # type: ignore[call-arg]


def test_oauth_rejects_none_algorithm(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_OAUTH_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("LF_HTTP_PUBLIC_URL", "https://lf.example.com/mcp")
    monkeypatch.setenv("LF_HTTP_OAUTH_ALGORITHMS", "none")
    with pytest.raises(ValueError, match="asymmetric"):
        Settings()  # type: ignore[call-arg]
