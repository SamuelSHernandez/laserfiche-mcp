"""Authentication strategies for Laserfiche.

Abstracts the difference between basic auth (self-hosted, simple) and OAuth
(self-hosted via LFDS or cloud). The client only sees ``apply(headers)``.
"""

from __future__ import annotations

import base64
import time
from abc import ABC, abstractmethod

import httpx

from .config import AuthMode, Settings


class AuthStrategy(ABC):
    """Adds whatever auth header(s) a request needs."""

    @abstractmethod
    async def apply(self, request: httpx.Request) -> None: ...


class BasicAuthStrategy(AuthStrategy):
    """HTTP Basic auth — simplest path for self-hosted dev/testing."""

    def __init__(self, username: str, password: str) -> None:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._header = f"Basic {token}"

    async def apply(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = self._header


class OAuthStrategy(AuthStrategy):
    """OAuth 2.0 client credentials with token caching.

    Stub for v1.1 — a working implementation needs the LFDS or Laserfiche Cloud
    token endpoint, which differs by deployment. The shape below is correct;
    the token URL needs to be filled in once we wire up cloud support.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    async def apply(self, request: httpx.Request) -> None:
        if not self._access_token or time.time() >= self._expires_at - 30:
            await self._refresh()
        request.headers["Authorization"] = f"Bearer {self._access_token}"

    async def _refresh(self) -> None:
        async with httpx.AsyncClient() as client:
            data = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
            if self._scope:
                data["scope"] = self._scope
            resp = await client.post(self._token_url, data=data)
            resp.raise_for_status()
            payload = resp.json()
            self._access_token = payload["access_token"]
            self._expires_at = time.time() + payload.get("expires_in", 3600)


def build_auth_strategy(settings: Settings) -> AuthStrategy:
    """Factory: pick the right strategy for the configured auth_mode."""
    if settings.auth_mode is AuthMode.BASIC:
        assert settings.username and settings.password  # validated upstream
        return BasicAuthStrategy(settings.username, settings.password)

    if settings.auth_mode is AuthMode.OAUTH:
        raise NotImplementedError(
            "OAuth auth mode is not yet wired up. "
            "Use LF_AUTH_MODE=basic for now, or contribute the LFDS token "
            "endpoint discovery in auth.py."
        )

    raise NotImplementedError(f"Unsupported auth mode: {settings.auth_mode}")
