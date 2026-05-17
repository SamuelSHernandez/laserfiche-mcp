"""Shared test fixtures, constants, and stubs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from laserfiche_mcp import _app, server
from laserfiche_mcp.auth import AuthStrategy
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings

# --- Constants --------------------------------------------------------------

# Repository base URL the test client points at. Every httpx_mock call in
# the tool-tests is built from this prefix so URL drift is one-line to fix.
LF_API_BASE = "https://lf.example.test/LFRepositoryAPI/v1/Repositories/demo"
LF_API_BASE_V2 = "https://lf.example.test/LFRepositoryAPI/v2/Repositories/demo"
_BASE = LF_API_BASE  # back-compat for older tests
_BASE_V1 = LF_API_BASE  # explicit v1 alias for client tests that toggle versions
_BASE_V2 = LF_API_BASE_V2

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
# Imported by tests as the canonical "what the fixture should extract to"
# string. If you change tests/fixtures/_generate.py SAMPLE_TEXT, change
# this constant too.
SAMPLE_PDF_TEXT = "Hello laserfiche-mcp test fixture."
SAMPLE_PDF_BYTES = (_FIXTURE_DIR / "sample_text.pdf").read_bytes()
SAMPLE_ENCRYPTED_PDF_BYTES = (_FIXTURE_DIR / "sample_encrypted.pdf").read_bytes()


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
    # Name pre-flight validators (Pass 1 security) opt-in for tests so
    # happy-path tests don't need to mock schema endpoints. Tests that
    # specifically exercise the validators monkeypatch this to True.
    "LF_VALIDATE_NAMES": "false",
}


# --- Stub auth used by every test that builds a LaserficheClient ------------


class _StubAuth(AuthStrategy):
    """No-op auth that stamps a static bearer header so requests parse."""

    async def apply(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = "Bearer test-token"


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def lf_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Set baseline LF_* env vars for the test, isolated via monkeypatch."""
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    yield dict(_BASE_ENV)


@pytest.fixture(autouse=True)
def _reset_settings(lf_env: dict[str, str]) -> None:
    """Clear the cached Settings before every test so env overrides take effect."""
    server._reset_settings_for_tests()


@pytest.fixture
async def patched_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[LaserficheClient]:
    """Replace the client accessor with a real LaserficheClient backed by httpx_mock.

    Sidesteps FastMCP's request-context machinery so tool functions can be
    awaited directly in tests. Tool modules look up the accessor via
    ``_app.get_client()`` (module-attribute access, not a snapshotted import
    binding), so a single ``monkeypatch.setattr(_app, "get_client", ...)``
    propagates to every caller.
    """
    settings = Settings()  # type: ignore[call-arg]
    async with LaserficheClient(settings, _StubAuth()) as client:

        def accessor() -> LaserficheClient:
            return client

        monkeypatch.setattr(_app, "get_client", accessor)
        # Back-compat alias for tests that still patch via ``server``.
        monkeypatch.setattr(server, "_client", accessor)
        yield client
