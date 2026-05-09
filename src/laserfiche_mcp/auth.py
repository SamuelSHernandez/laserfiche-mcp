"""Authentication strategies for Laserfiche.

Abstracts the difference between basic auth (self-hosted, simple) and OAuth
client-credentials (self-hosted via LFDS or cloud). The client only sees
``apply(request)``.
"""

from __future__ import annotations

import base64
import logging
import time
from abc import ABC, abstractmethod

import httpx
from pydantic import SecretStr

from .config import AuthMode, Settings

logger = logging.getLogger("laserfiche_mcp.auth")


class AuthStrategy(ABC):
    """Adds whatever auth header(s) a request needs."""

    @abstractmethod
    async def apply(self, request: httpx.Request) -> None: ...


class BasicAuthStrategy(AuthStrategy):
    """HTTP Basic auth — simplest path for self-hosted."""

    def __init__(self, username: str, password: SecretStr) -> None:
        token = base64.b64encode(
            f"{username}:{password.get_secret_value()}".encode()
        ).decode()
        # Encoded once; the SecretStr is dropped after this — we keep only
        # the base64 form, which is no more sensitive than the original.
        self._header = f"Basic {token}"

    async def apply(self, request: httpx.Request) -> None:
        request.headers["Authorization"] = self._header


class OAuthStrategy(AuthStrategy):
    """OAuth 2.0 client_credentials grant with token caching.

    Refreshes ~30 seconds before expiry. Re-uses one short-lived
    ``httpx.AsyncClient`` per refresh; tokens are kept in memory only and
    are dropped on server restart.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: SecretStr,
        scope: str | None = None,
        verify_ssl: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._verify_ssl = verify_ssl
        self._timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    async def apply(self, request: httpx.Request) -> None:
        if not self._access_token or time.time() >= self._expires_at - 30:
            await self._refresh()
        request.headers["Authorization"] = f"Bearer {self._access_token}"

    async def _refresh(self) -> None:
        logger.debug("Refreshing OAuth access token from %s", self._token_url)
        async with httpx.AsyncClient(
            verify=self._verify_ssl,
            timeout=self._timeout_seconds,
        ) as client:
            data = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret.get_secret_value(),
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
        assert (
            settings.oauth_token_url
            and settings.client_id
            and settings.client_secret
        )  # validated upstream
        return OAuthStrategy(
            token_url=str(settings.oauth_token_url),
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            scope=settings.oauth_scope,
            verify_ssl=settings.verify_ssl,
            timeout_seconds=settings.request_timeout_seconds,
        )

    # Settings validation rejects API_KEY before we get here.
    raise NotImplementedError(f"Unsupported auth mode: {settings.auth_mode}")
