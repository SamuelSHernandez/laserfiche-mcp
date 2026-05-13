"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# Reasonable defaults for tests; individual tests override as needed.
_BASE_ENV: dict[str, str] = {
    "LF_DEPLOYMENT_MODE": "self_hosted",
    "LF_REPO_API_URL": "https://lf.example.test/LFRepositoryAPI",
    "LF_REPOSITORY_ID": "demo",
    "LF_API_VERSION": "v1",
    "LF_AUTH_MODE": "password",
    "LF_USERNAME": "svc",
    "LF_PASSWORD": "secret",
    "LF_READ_ONLY": "true",
    "LF_RETRY_ATTEMPTS": "0",
}


@pytest.fixture
def lf_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Set baseline LF_* env vars for the test, isolated via monkeypatch."""
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    yield dict(_BASE_ENV)
